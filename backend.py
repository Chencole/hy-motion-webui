"""Visual UI adapter for the existing local HY-Motion command line.

This module never imports torch and never downloads or starts inference during inspection.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sys

APP_ROOT = Path(__file__).resolve().parent
MODEL_NAME = 'Tencent HY-Motion-1.0 (standard, CPU)'
WEIGHT_SIZES = {
    'repo/ckpts/tencent/HY-Motion-1.0/latest.ckpt': 4171703514,
    'repo/ckpts/Qwen3-8B/model-00001-of-00005.safetensors': 3996250744,
    'repo/ckpts/Qwen3-8B/model-00002-of-00005.safetensors': 3993160032,
    'repo/ckpts/Qwen3-8B/model-00003-of-00005.safetensors': 3959604768,
    'repo/ckpts/Qwen3-8B/model-00004-of-00005.safetensors': 3187841392,
    'repo/ckpts/Qwen3-8B/model-00005-of-00005.safetensors': 1244659840,
    'repo/ckpts/clip-vit-large-patch14/model.safetensors': 1710540580,
}
SUPPORT_FILES = (
    'src/hy_motion_cpu/cli.py',
    'scripts/staged_infer.py',
    'repo/hymotion/pipeline/motion_diffusion.py',
    'repo/ckpts/tencent/HY-Motion-1.0/config.yml',
    'repo/ckpts/Qwen3-8B/config.json',
    'repo/ckpts/Qwen3-8B/tokenizer.json',
    'repo/ckpts/Qwen3-8B/tokenizer_config.json',
    'repo/ckpts/Qwen3-8B/model.safetensors.index.json',
    'repo/ckpts/clip-vit-large-patch14/config.json',
    'repo/ckpts/clip-vit-large-patch14/tokenizer_config.json',
    'repo/ckpts/clip-vit-large-patch14/vocab.json',
    'repo/ckpts/clip-vit-large-patch14/merges.txt',
    'repo/stats/Mean.npy', 'repo/stats/Std.npy',
    'repo/assets/wooden_models/boy_Rigging_smplx_tex.fbx',
    'repo/scripts/gradio/templates/index_wooden_static.html',
    'repo/scripts/gradio/static/assets/dump_wooden/v_template.bin',
    'repo/scripts/gradio/static/assets/dump_wooden/j_template.bin',
    'repo/scripts/gradio/static/assets/dump_wooden/skinWeights.bin',
    'repo/scripts/gradio/static/assets/dump_wooden/skinIndice.bin',
    'repo/scripts/gradio/static/assets/dump_wooden/kintree.bin',
    'repo/scripts/gradio/static/assets/dump_wooden/faces.bin',
)
LIMITATIONS = [
    'CPU inference is slow and requires substantial free RAM; small free hosting tiers cannot run the full model.',
    'The existing local installation was checked; full generation has not yet been verified.',
    'The official model predicts body motion, not detailed finger grasping or weapon contact.',
    'The HTML preview uses upstream web assets; model inference itself uses local files.',
    'Tencent HY-Motion has its own Community License and territory/use conditions; see THIRD_PARTY_NOTICES.md.',
]

def backend_root() -> Path:
    configured = os.environ.get('MOTION_BACKEND_ROOT')
    if configured:
        return Path(configured).expanduser().resolve()
    sibling = APP_ROOT.parent / 'HYMotion'
    return sibling.resolve()

def backend_python(root: Path) -> Path:
    return root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')

def configured_device() -> str:
    device = os.environ.get('MOTION_DEVICE', 'cpu')
    if device not in ('cpu', 'cuda:0'):
        raise ValueError('MOTION_DEVICE must be cpu or cuda:0')
    return device


def configured_text_device(device: str) -> str:
    text_device = os.environ.get('MOTION_TEXT_DEVICE', 'cuda-offload' if device == 'cuda:0' else 'cpu')
    if text_device not in ('cpu', 'cuda-offload'):
        raise ValueError('MOTION_TEXT_DEVICE must be cpu or cuda-offload')
    return text_device

def inspect_backend() -> dict:
    if os.environ.get('MOTION_BACKEND', 'local') == 'fc':
        from remote_client import inspect_remote
        return inspect_remote()
    root = backend_root()
    python = backend_python(root)
    try:
        device = configured_device()
        text_device = configured_text_device(device)
    except ValueError as error:
        return {
            'ready': False, 'message': str(error), 'root': str(root), 'python': str(python),
            'model': 'Tencent HY-Motion-1.0 (standard, device configuration invalid)',
            'device': None, 'requested_device': os.environ.get('MOTION_DEVICE'),
            'text_device': None, 'requested_text_device': os.environ.get('MOTION_TEXT_DEVICE'), 'limitations': list(LIMITATIONS),
            'missing': [str(error)], 'inference_verified': False,
        }
    missing = []
    for name in SUPPORT_FILES:
        path = root / name
        if not path.is_file() or (path.stat().st_size < 1024 and path.read_bytes().startswith(b'version https://git-lfs.github.com/spec/v1')):
            missing.append(name)
    missing.extend(name for name, size in WEIGHT_SIZES.items()
                   if not (root / name).is_file() or (root / name).stat().st_size != size)
    if not python.is_file():
        missing.insert(0, str(python.relative_to(root)))
    venv_config = root / '.venv/pyvenv.cfg'
    config_text = venv_config.read_text(encoding='utf-8') if venv_config.is_file() else ''
    if not re.search(r'^version(?:_info)?\s*=\s*3\.11\.', config_text, re.MULTILINE):
        missing.append('compatible Python 3.11 environment')
    if device == 'cuda:0' or text_device == 'cuda-offload':
        try:
            adapter = json.loads((root / 'runtime-adapter.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            adapter = {}
        devices = adapter.get('motion_devices') if isinstance(adapter, dict) else None
        text_devices = adapter.get('text_devices') if isinstance(adapter, dict) else None
        if (not isinstance(adapter, dict) or type(adapter.get('schema')) is not int
                or adapter['schema'] != 1 or not isinstance(devices, list)
                or not all(isinstance(value, str) for value in devices) or device not in devices
                or (text_device == 'cuda-offload' and (not isinstance(text_devices, list)
                    or not all(isinstance(value, str) for value in text_devices) or text_device not in text_devices))):
            missing.append('staged CUDA adapter; see deploy/windows-gpu/README.md')
    ready = not missing
    message = ('Official model files and CPU environment found; full inference is not yet verified.' if ready
               else 'Existing HY-Motion installation not found or incomplete. Set MOTION_BACKEND_ROOT to its directory.')
    limitations = list(LIMITATIONS)
    model = MODEL_NAME
    if device == 'cuda:0':
        model = 'Tencent HY-Motion-1.0 (standard, CUDA motion / ' + ('CUDA text offload)' if text_device == 'cuda-offload' else 'CPU text)')
        limitations[0] = 'Text weights remain in RAM; CUDA text offload executes one module at a time before motion sampling. Run only one GPU model at a time on an 8 GB card.' if text_device == 'cuda-offload' else 'Text encoding uses CPU and substantial RAM; motion sampling uses CUDA. CPU BF16 can be very slow without native CPU BF16 support.'
        limitations[1] = 'File inspection does not verify CUDA or inference; run the GPU check and a real generation after installation.'
        if ready:
            message = 'Official model files and staged CUDA adapter found; GPU inference requires runtime verification.'
    return {
        'ready': ready,
        'message': message,
        'root': str(root), 'python': str(python), 'model': model, 'device': device,
        'requested_device': device,
        'text_device': text_device, 'requested_text_device': text_device, 'limitations': limitations, 'missing': missing,
        'inference_verified': False,
    }

def _integer(config: dict, key: str, default: int, positive: bool = False) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or (positive and value < 1):
        raise ValueError(f'{key} must be a {"positive " if positive else ""}integer')
    return value

def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError('Configuration must be an object')
    prompt = config.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip() or '\0' in prompt or len(prompt) > 4000:
        raise ValueError('prompt must contain text without NUL')
    seconds = config.get('seconds', 4.0)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 12:
        raise ValueError('seconds must be in (0, 12]')
    result = dict(prompt=prompt, seconds=float(seconds),
                seed=_integer(config, 'seed', 42),
                steps=_integer(config, 'steps', 50, positive=True),
                threads=_integer(config, 'threads', 8, positive=True))
    if not 0 <= result['seed'] <= 2**32 - 1:
        raise ValueError('seed must be between 0 and 4294967295')
    if result['steps'] > 200 or result['threads'] > 64:
        raise ValueError('steps must be at most 200; threads at most 64')
    return result


def command_for(job_dir: Path, parameters: dict, state: dict) -> list[str]:
    if os.environ.get('MOTION_BACKEND', 'local') == 'fc':
        return [sys.executable, '-u', str(APP_ROOT / 'remote_client.py'),
                '--job-id', job_dir.name, '--request', str(job_dir / 'request.json'),
                '--output', str(job_dir / 'output')]
    command = [state['python'], '-u', str(Path(state['root']) / 'src/hy_motion_cpu/cli.py'),
            '--prompt', parameters['prompt'], '--seconds', str(parameters['seconds']),
            '--seed', str(parameters['seed']), '--steps', str(parameters['steps']),
            '--threads', str(parameters['threads']), '--output', str(job_dir / 'output')]
    device = configured_device()
    text_device = configured_text_device(device)
    if device == 'cuda:0':
        command += ['--device', 'cuda:0']
    if device == 'cuda:0' or text_device != 'cpu':
        command += ['--text-device', text_device]
    return command


def resume_command(job_dir: Path, config: dict) -> list[str]:
    parameters = validate_config(config)
    stored = json.loads((job_dir / 'request.json').read_text(encoding='utf-8'))
    if stored != parameters:
        raise ValueError('Saved request differs from queued parameters; refusing to run')
    state = inspect_backend()
    if not state['ready']:
        raise RuntimeError(state['message'])
    return command_for(job_dir, parameters, state)

def build_command(job_dir: Path, config: dict, input_path: Path | None = None) -> list[str]:
    if input_path is not None:
        raise ValueError('HY-Motion accepts text, not a video upload')
    parameters = validate_config(config)
    state = inspect_backend()
    if not state['ready']:
        raise RuntimeError(state['message'])
    job_dir = Path(job_dir).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    request_path = job_dir / 'request.json'
    with request_path.open('x', encoding='utf-8') as stream:
        json.dump(parameters, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return command_for(job_dir, parameters, state)
