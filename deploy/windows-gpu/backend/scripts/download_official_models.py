"""Download pinned upstream HY-Motion standard and its required text encoders."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]
os.environ['HF_HOME'] = str(ROOT / 'cache/huggingface')
os.environ['HF_HUB_DOWNLOAD_TIMEOUT'] = '120'
os.environ['HF_HUB_ETAG_TIMEOUT'] = '60'
os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
from huggingface_hub import HfApi, hf_hub_download

MODELS = [
    ('tencent/HY-Motion-1.0', '620dd559f8d964aac2f82f1204fe6a35ad8ad14d',
     ROOT / 'repo/ckpts/tencent', lambda n: n.startswith('HY-Motion-1.0/') or n in ('LICENSE.txt', 'README.md')),
    ('Qwen/Qwen3-8B', 'b968826d9c46dd6066d109eabc6255188de91218',
     ROOT / 'repo/ckpts/Qwen3-8B', lambda n: n != '.gitattributes'),
    ('openai/clip-vit-large-patch14', '32bd64288804d66eefd0ccbe215aa642df71cc41',
     ROOT / 'repo/ckpts/clip-vit-large-patch14', lambda n: not n.endswith(('.bin', '.h5', '.msgpack')) and n != '.gitattributes'),
]

def fetch(task):
    repo, revision, directory, name, size, expected = task
    for attempt in range(5):
        try:
            path = Path(hf_hub_download(repo, name, revision=revision, local_dir=directory))
            break
        except Exception as error:
            if attempt == 4:
                raise
            print(f'Resuming {repo}/{name} after {type(error).__name__}; retry {attempt + 1}/4', flush=True)
            time.sleep(3)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    sha = digest.hexdigest()
    if path.stat().st_size != size or (expected and sha != expected):
        raise RuntimeError(f'Integrity mismatch: {path}')
    print(f'VERIFIED {repo}/{name}: {size} bytes', flush=True)
    return dict(repo=repo, revision=revision, path=str(path), size_bytes=size, sha256=sha,
                upstream_sha256=expected)

if __name__ == '__main__':
    tasks = []
    api = HfApi()
    for repo, revision, directory, include in MODELS:
        info = api.model_info(repo, revision=revision, files_metadata=True)
        for entry in info.siblings:
            if include(entry.rfilename):
                expected = entry.lfs.sha256 if entry.lfs else None
                tasks.append((repo, revision, directory, entry.rfilename, entry.size, expected))
    print(f'Downloading/verifying {len(tasks)} files, {sum(t[4] for t in tasks):,} bytes', flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        files = list(pool.map(fetch, tasks))
    (ROOT / 'official-model-manifest.json').write_text(json.dumps(dict(files=files, verified_at=time.time()), indent=2), encoding='utf-8')
    print('All required official weights verified.', flush=True)
