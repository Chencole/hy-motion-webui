"""Validate local model assets and the requested compute device without inference."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_checks(requested_device, report):
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', USE_HF_MODELS='0')
    sys.path.insert(0, str(ROOT / 'repo'))
    os.chdir(ROOT / 'repo')
    import numpy as np
    import torch
    from transformers import AutoConfig, AutoTokenizer, CLIPTokenizer
    from safetensors import safe_open
    from hymotion.utils.smplh2woodfbx import SMPLH2WoodFBX
    from staged_infer import motion_device

    device = motion_device(torch, requested_device)
    # An actual device operation catches unusable drivers and missing CUDA binaries.
    with torch.inference_mode():
        a = torch.tensor([[1., 2.], [3., 4.]], device=device)
        product = a @ a
        torch.testing.assert_close(product.cpu(), torch.tensor([[7., 10.], [15., 22.]]))
    if device.type == 'cuda':
        torch.cuda.synchronize(device)
    report.update(device=str(device), actual_device=str(device), cuda=torch.version.cuda,
                  gpu=torch.cuda.get_device_name(device) if device.type == 'cuda' else None)
    model = ROOT / 'repo/ckpts/tencent/HY-Motion-1.0'
    if (model / 'latest.ckpt').stat().st_size != 4171703514:
        raise RuntimeError('Missing/incomplete official standard HY-Motion checkpoint')
    if not (model / 'config.yml').is_file():
        raise RuntimeError('Missing official model config')
    qwen = ROOT / 'repo/ckpts/Qwen3-8B'
    clip = ROOT / 'repo/ckpts/clip-vit-large-patch14'
    expected = [3996250744, 3993160032, 3959604768, 3187841392, 1244659840]
    tensors = 0
    for index, size in enumerate(expected, 1):
        path = qwen / f'model-{index:05d}-of-00005.safetensors'
        if path.stat().st_size != size:
            raise RuntimeError(f'Incomplete Qwen shard: {path.name}')
        with safe_open(str(path), framework='pt', device='cpu') as handle:
            tensors += len(handle.keys())
    if (clip / 'model.safetensors').stat().st_size != 1710540580:
        raise RuntimeError('Missing/incomplete CLIP checkpoint')
    assert AutoConfig.from_pretrained(qwen, local_files_only=True).hidden_size == 4096
    for tokenizer in (AutoTokenizer.from_pretrained(qwen, local_files_only=True),
                      CLIPTokenizer.from_pretrained(clip, local_files_only=True)):
        assert len(tokenizer('A person walks forward.')['input_ids']) > 0
    for name in ('Mean.npy', 'Std.npy'):
        values = np.load(ROOT / 'repo/stats' / name, allow_pickle=False)
        assert values.shape == (201,) and np.isfinite(values).all()
    assert len(SMPLH2WoodFBX().smplh_to_fbx_mapping) == 52
    report.update(qwen_tensor_keys=tensors,
                  packages={name: importlib.metadata.version(name) for name in
                            ('torch', 'torchvision', 'transformers', 'fbxsdkpy', 'numpy')})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', choices=('cpu', 'cuda:0'), default='cpu')
    args = parser.parse_args()
    report = dict(status='running', started_at=time.time(), requested_device=args.device,
                  actual_device=None, device=None, cuda=None, gpu=None,
                  inference_tested=False, python=sys.executable)
    report_path = ROOT / 'logs/installation-check.json'
    write_report(report_path, report)
    try:
        run_checks(args.device, report)
        report['status'] = 'installation_checks_passed'
    except Exception as error:
        report.update(status='failed', error_type=type(error).__name__, error=str(error))
        raise
    finally:
        report['checked_at'] = time.time()
        write_report(report_path, report)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
