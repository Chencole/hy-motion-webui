"""Two-stage official-model adapter: CPU text encoding and CPU/CUDA motion."""
import argparse
from datetime import datetime
import hashlib
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]

# A separate process releases its CUDA context before CPU text encoding starts.
# It imports no model code and validates an actual operation, not only discovery.
CUDA_PREFLIGHT = '''import sys
import torch
if not torch.cuda.is_available():
    raise RuntimeError("CUDA requested but unavailable; refusing a silent CPU fallback.")
device = torch.device(sys.argv[1])
torch.cuda.set_device(device)
with torch.inference_mode():
    matrix = torch.tensor([[1., 2.], [3., 4.]], device=device)
    torch.testing.assert_close((matrix @ matrix).cpu(), torch.tensor([[7., 10.], [15., 22.]]))
torch.cuda.synchronize(device)
print("CUDA preflight passed:", device, torch.cuda.get_device_name(device), flush=True)
'''

def main():
    parser = argparse.ArgumentParser(description='Official HY-Motion standard weights; separate CPU text encoding and CPU/CUDA motion sampling.')
    parser.add_argument('--prompt', help='English action description')
    parser.add_argument('--seconds', type=float, default=4.0, help='Motion duration, 0 < seconds <= 12 (default 4)')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--steps', type=int, default=50, help='Official default 50')
    parser.add_argument('--threads', type=int, default=8)
    parser.add_argument('--device', choices=('cpu', 'cuda:0'), default='cpu', help='Motion sampling device; text encoding stays on CPU')
    parser.add_argument('--output', type=Path, help='New output folder (automatic timestamp by default)')
    parser.add_argument('--refresh-text', action='store_true', help='Recompute the cached text encoding')
    parser.add_argument('--check', action='store_true', help='Verify installed files/imports without loading or generating from the model')
    args = parser.parse_args()
    if args.check:
        return subprocess.call([sys.executable, str(ROOT / 'scripts/check_installation.py'), '--device', args.device], cwd=ROOT)
    if not args.prompt or not args.prompt.strip():
        parser.error('--prompt is required unless --check is used')
    if not 0 < args.seconds <= 12 or args.steps < 1 or args.threads < 1:
        parser.error('--seconds must be in (0,12], and --steps/--threads must be positive')
    output = (args.output or ROOT / 'outputs' / datetime.now().strftime('%Y%m%d_%H%M%S_%f')).resolve()
    if output.exists():
        parser.error(f'Output already exists; choose a new folder: {output}')
    text_key = hashlib.sha256(args.prompt.encode('utf-8')).hexdigest()
    cache = ROOT / 'cache/text_features' / f'{text_key}.pt'
    command = [sys.executable, str(ROOT / 'scripts/staged_infer.py')]
    common = ['--cache-path', str(cache), '--threads', str(args.threads), '--device', args.device]
    try:
        if args.device == 'cuda:0':
            subprocess.run([sys.executable, '-c', CUDA_PREFLIGHT, args.device], cwd=ROOT, check=True)
        if args.refresh_text or not cache.exists():
            subprocess.run(command + ['encode', '--prompt', args.prompt] + common, cwd=ROOT, check=True)
        subprocess.run(command + ['generate', '--prompt', args.prompt, '--duration', str(args.seconds),
                       '--seed', str(args.seed), '--steps', str(args.steps), '--output-dir', str(output)] + common,
                       cwd=ROOT, check=True)
    except subprocess.CalledProcessError as error:
        return error.returncode or 1
    print(f'Output: {output}')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
