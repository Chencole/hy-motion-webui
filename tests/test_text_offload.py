"""Offload error/metadata contracts, without importing Torch or loading weights."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/windows-gpu/backend/scripts/staged_infer.py'
spec = importlib.util.spec_from_file_location('text_offload_under_test', SCRIPT)
staged = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staged)


class Device(str):
    @property
    def type(self):
        return self.split(':')[0]


class Tensor:
    def __init__(self, shape, dtype, device, value=4):
        self.shape, self.dtype, self.device, self.value = shape, dtype, Device(device), value

    def detach(self):
        return self

    def cpu(self):
        return Tensor(self.shape, self.dtype, 'cpu', self.value)

    def contiguous(self):
        return self

    def item(self):
        return self.value


class Leaf:
    def __init__(self):
        self.callbacks = []
        self.handles = []

    def register_forward_hook(self, callback):
        self.callbacks.append(callback)
        handle = SimpleNamespace(remove=Mock())
        self.handles.append(handle)
        return handle


class TextOffloadTests(unittest.TestCase):
    def fixture(self, mode='cuda-offload', available=True, observe=True, outputs_device=None):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        for name in ('qwen', 'clip'):
            (root / name).mkdir()
            (root / name / 'config.json').write_text('{}')
        args = SimpleNamespace(qwen_path=root / 'qwen', clip_path=root / 'clip', prompt='A person walks.',
                               text_device=mode, cache_path=root / 'cache/features.pt')
        qwen_leaf, clip_leaf = Leaf(), Leaf()
        qwen = SimpleNamespace(parameters=lambda: iter([SimpleNamespace(dtype='torch.bfloat16')]),
                               model=SimpleNamespace(embed_tokens=qwen_leaf))
        clip = SimpleNamespace(parameters=lambda: iter([SimpleNamespace(dtype='torch.float32')]),
                               text_model=SimpleNamespace(embeddings=SimpleNamespace(token_embedding=clip_leaf)))
        encoder = SimpleNamespace(llm_text_encoder=qwen, sentence_emb_text_encoder=clip, eval=Mock())
        original_get_device = Mock(return_value=Device('cpu'))
        official = SimpleNamespace(LLM_ENCODER_LAYOUT={'qwen3': {}}, SENTENCE_EMB_LAYOUT={'clipl': {}},
                                   HYTextModel=Mock(return_value=encoder), get_module_device=original_get_device)

        def encode(prompts):
            self.assertEqual(prompts, [args.prompt])
            selected = official.get_module_device(encoder)
            expected = 'cuda:0' if mode == 'cuda-offload' else 'cpu'
            self.assertEqual(str(selected), expected)
            # The temporary helper override must not change unrelated models.
            self.assertEqual(official.get_module_device(object()), Device('cpu'))
            if observe:
                for leaf, shape, dtype in ((qwen_leaf, (1, 200, 4096), 'torch.bfloat16'),
                                           (clip_leaf, (1, 8, 768), 'torch.float32')):
                    for hook in leaf.callbacks:
                        hook(leaf, (), Tensor(shape, dtype, selected))
            actual = outputs_device or selected
            return (Tensor((1, 1, 768), 'torch.float32', actual),
                    Tensor((1, 128, 4096), 'torch.bfloat16', actual),
                    Tensor((1,), 'torch.int64', actual))

        encoder.encode = Mock(side_effect=encode)
        cuda = SimpleNamespace(is_available=Mock(return_value=available), set_device=Mock(),
                               reset_peak_memory_stats=Mock(), get_device_name=Mock(return_value='mock GPU'),
                               synchronize=Mock(), max_memory_allocated=Mock(return_value=1024),
                               max_memory_reserved=Mock(return_value=2048))
        torch = SimpleNamespace(Tensor=Tensor, bfloat16='torch.bfloat16', int64='torch.int64',
                                device=Device, cuda=cuda, version=SimpleNamespace(cuda='12.4'),
                                inference_mode=contextlib.nullcontext,
                                isfinite=lambda tensor: SimpleNamespace(all=lambda: SimpleNamespace(item=lambda: True)),
                                save=Mock(side_effect=lambda value, path: Path(path).write_bytes(b'mocked-cache')))
        accelerate = SimpleNamespace(cpu_offload=Mock())
        package = ModuleType('hymotion.network.text_encoders')
        package.text_encoder = official
        modules = {'torch': torch, 'accelerate': accelerate,
                   'hymotion': ModuleType('hymotion'), 'hymotion.network': ModuleType('hymotion.network'),
                   'hymotion.network.text_encoders': package}
        patcher = patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        metadata = {'device': None, 'text_device': None, 'requested_text_device': mode}
        return SimpleNamespace(args=args, metadata=metadata, encoder=encoder, official=official,
                               original_get_device=original_get_device, qwen=qwen, clip=clip,
                               qwen_leaf=qwen_leaf, clip_leaf=clip_leaf, torch=torch, accelerate=accelerate)

    def invoke(self, fixture):
        with contextlib.redirect_stdout(io.StringIO()):
            staged.encode(fixture.args, fixture.metadata)

    def assert_restored(self, fixture):
        self.assertIs(fixture.official.get_module_device, fixture.original_get_device)
        for leaf in (fixture.qwen_leaf, fixture.clip_leaf):
            for handle in leaf.handles:
                handle.remove.assert_called_once_with()

    def test_observed_cuda_execution_saves_cpu_features_with_original_dtypes(self):
        f = self.fixture()
        self.invoke(f)
        self.assertEqual([call.args[0] for call in f.accelerate.cpu_offload.call_args_list], [f.qwen, f.clip])
        for call in f.accelerate.cpu_offload.call_args_list:
            self.assertEqual(call.kwargs, {'execution_device': Device('cuda:0')})
        payload = f.torch.save.call_args.args[0]
        self.assertEqual(f.metadata['device'], 'cuda:0')
        self.assertEqual(f.metadata['text_device'], 'cuda:0')
        self.assertEqual(payload['metadata']['text_execution'], 'cuda-module-offload')
        self.assertEqual(payload['metadata']['text_weight_storage'], 'cpu')
        self.assertEqual(set(f.metadata['text_module_outputs']), {'qwen_embedding', 'clip_embedding'})
        self.assertTrue(all(value['device'] == 'cuda:0' for value in f.metadata['text_module_outputs'].values()))
        self.assertTrue(all(str(value.device) == 'cpu' for value in payload['features'].values()))
        self.assertEqual([value.dtype for value in payload['features'].values()],
                         ['torch.float32', 'torch.bfloat16', 'torch.int64'])
        self.assertEqual(f.metadata['cuda_peak_allocated_bytes'], 1024)
        self.assertTrue(f.args.cache_path.is_file())
        self.assert_restored(f)

    def test_forward_failure_restores_helper_and_observers_without_cache_or_retry(self):
        f = self.fixture()
        error = RuntimeError('CUDA out of memory')
        f.encoder.encode.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            self.invoke(f)
        self.assertIs(caught.exception, error)
        f.encoder.encode.assert_called_once()
        f.torch.save.assert_not_called()
        self.assertFalse(f.args.cache_path.exists())
        self.assertIsNone(f.metadata['device'])
        self.assert_restored(f)

    def test_partial_observer_setup_failure_restores_helper_and_registered_handle(self):
        f = self.fixture()
        error = RuntimeError('CLIP observer registration failed')
        f.clip_leaf.register_forward_hook = Mock(side_effect=error)
        with self.assertRaises(RuntimeError) as caught:
            self.invoke(f)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(f.qwen_leaf.handles), 1)
        f.encoder.encode.assert_not_called()
        f.torch.save.assert_not_called()
        self.assertIsNone(f.metadata['device'])
        self.assert_restored(f)

    def test_cuda_unavailable_rejects_before_encoding_without_cpu_fallback(self):
        f = self.fixture(available=False)
        with self.assertRaisesRegex(RuntimeError, 'refusing a silent CPU fallback'):
            self.invoke(f)
        f.encoder.encode.assert_not_called()
        f.accelerate.cpu_offload.assert_not_called()
        f.torch.save.assert_not_called()
        self.assertIsNone(f.metadata['device'])
        self.assert_restored(f)

    def test_unobserved_execution_is_rejected_before_cache_write(self):
        f = self.fixture(observe=False)
        with self.assertRaisesRegex(RuntimeError, 'execution was not observed'):
            self.invoke(f)
        f.torch.save.assert_not_called()
        self.assertIsNone(f.metadata['device'])
        self.assert_restored(f)

    def test_cpu_outputs_from_requested_offload_are_rejected(self):
        f = self.fixture(outputs_device='cpu')
        with self.assertRaisesRegex(RuntimeError, 'did not remain on the requested CUDA device'):
            self.invoke(f)
        f.torch.save.assert_not_called()
        self.assertIsNone(f.metadata['device'])
        self.assert_restored(f)

    def test_cpu_mode_never_offloads_or_checks_cuda(self):
        f = self.fixture(mode='cpu')
        self.invoke(f)
        f.accelerate.cpu_offload.assert_not_called()
        f.torch.cuda.is_available.assert_not_called()
        self.assertEqual(f.metadata['device'], 'cpu')
        self.assertEqual(f.metadata['text_execution'], 'cpu')
        self.assertNotIn('text_module_outputs', f.metadata)
        self.assert_restored(f)


if __name__ == '__main__':
    unittest.main()
