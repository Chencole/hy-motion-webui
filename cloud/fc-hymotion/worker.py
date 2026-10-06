"""One official GPU generation. Imported modules do not initialize torch or models."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from contract import FILES, REQUIRED_FILES, UPSTREAM_COMMIT, canonical_bytes


def generate_official_outputs(runtime, config, source, work):
    """Use a real repository-relative folder, as the official HTML reader requires."""
    source, work = Path(source).resolve(), Path(work).resolve()
    output_root = source / 'output'
    output_root.mkdir(exist_ok=True)
    if output_root.is_symlink() or not output_root.resolve().is_relative_to(source):
        raise RuntimeError('Official output directory must be a real directory inside its source tree')
    with tempfile.TemporaryDirectory(dir=output_root, prefix='fc-') as temporary:
        generated = Path(temporary).resolve()
        relative_output = generated.relative_to(source).as_posix()
        html, _fbx_files, model_output = runtime.generate_motion(
            text=config['prompt'], original_text=config['prompt'], seeds_csv=str(config['seed']),
            duration=config['seconds'], cfg_scale=5.0, output_format='fbx', output_dir=relative_output,
            output_filename='motion', use_special_game_feat=False)
        if not isinstance(html, str) or not html.strip() or 'Error generating visualization' in html:
            raise RuntimeError('Official runtime returned invalid visualization HTML; refusing a false success')
        # Copy only the official artifact allowlist; worker report, preview and service log are owned here.
        for name in FILES:
            if name in ('preview.html', 'generation_report.json', 'generation.log'):
                continue
            artifact = generated / name
            if artifact.is_file():
                shutil.copy2(artifact, work / name)
    return html, model_output


def run(work):
    work = Path(work).resolve()
    config = json.loads((work / 'request.json').read_text(encoding='utf-8'))
    source = Path(os.getenv('HYMOTION_OFFICIAL_SOURCE', '/opt/hymotion')).resolve()
    assets = Path(os.getenv('HYMOTION_ASSETS_MOUNT', '/mnt/hymotion-assets')).resolve()
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True, timeout=10).strip()
    if commit != UPSTREAM_COMMIT:
        raise RuntimeError('Official HY-Motion source revision does not match this worker')
    if (source / 'ckpts').resolve() != assets / 'ckpts':
        raise RuntimeError('Official relative checkpoint paths must resolve to the persistent model mount')
    os.chdir(source)
    sys.path.insert(0, str(source))
    os.environ.update({'USE_HF_MODELS': '0', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                       'HF_HUB_DISABLE_IMPLICIT_TOKEN': '1', 'DISABLE_PROMPT_ENGINEERING': 'True',
                       'OMP_NUM_THREADS': str(config['threads']), 'MKL_NUM_THREADS': str(config['threads'])})
    # All heavy imports and CUDA checks occur only inside an actual paid generation request.
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError('A CUDA GPU is required; CPU fallback is not permitted')
    torch.set_num_threads(config['threads'])
    from hymotion.utils.t2m_runtime import T2MRuntime
    started = time.monotonic()
    runtime = T2MRuntime(config_path=str(assets / 'ckpts/tencent/HY-Motion-1.0/config.yml'),
                         ckpt_name=str(assets / 'ckpts/tencent/HY-Motion-1.0/latest.ckpt'),
                         device_ids=[0], disable_prompt_engineering=True)
    if not runtime.fbx_available:
        raise RuntimeError('Official FBX exporter is unavailable; refusing a partial-format success')
    for pipeline in runtime.pipelines:
        pipeline.validation_steps = config['steps']
    html, model_output = generate_official_outputs(runtime, config, source, work)
    # Files above are the official runtime's NPZ/FBX/metadata, without an additional motion correction pass.
    (work / 'preview.html').write_text(html, encoding='utf-8')
    report = {'backend': 'tencent_hymotion_official', 'model': 'Tencent HY-Motion-1.0 standard',
              'upstream_commit': commit, 'device': 'cuda:0', 'prompt_rewriting': False,
              'duration_estimation': False, 'extra_postprocessing': False, 'ai_acceptance': False,
              'config': config, 'cfg_scale': 5.0, 'frames': int(model_output['rot6d'].shape[1]),
              'fps': 30, 'elapsed_seconds': round(time.monotonic() - started, 3),
              'torch_version': torch.__version__}
    (work / 'generation_report.json').write_bytes(canonical_bytes(report))
    for name in REQUIRED_FILES:
        if not (work / name).is_file() or not (work / name).stat().st_size:
            raise RuntimeError('Official runtime did not produce all required artifacts')


if __name__ == '__main__':
    run(sys.argv[1])
