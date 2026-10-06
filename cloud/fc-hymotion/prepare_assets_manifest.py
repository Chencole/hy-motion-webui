"""Verify existing local official assets and emit a transfer plan. Never upload or download."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PureWindowsPath
from urllib.parse import quote


def prepare(root, verify_hashes=False):
    original = json.loads((root / 'official-model-manifest.json').read_text(encoding='utf-8'))
    files = []
    for item in original['files']:
        old_path = PureWindowsPath(item['path'])
        try:
            ckpt_index = old_path.parts.index('ckpts')
        except ValueError:
            continue
        relative = Path(*old_path.parts[ckpt_index + 1:])
        local = root / 'repo/ckpts' / relative
        if not local.is_file() or local.stat().st_size != item['size_bytes']:
            raise ValueError(f'Local asset is missing or its size changed: {relative}')
        if verify_hashes:
            with local.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != item['sha256']:
                raise ValueError(f'Local asset SHA-256 changed: {relative}')
        if item['repo'] == 'tencent/HY-Motion-1.0':
            remote_path = relative.relative_to('tencent').as_posix()
        elif item['repo'] == 'Qwen/Qwen3-8B':
            remote_path = relative.relative_to('Qwen3-8B').as_posix()
        elif item['repo'] == 'openai/clip-vit-large-patch14':
            remote_path = relative.relative_to('clip-vit-large-patch14').as_posix()
        else:
            raise ValueError('Unexpected upstream model repository')
        files.append({'local_relative_path': 'repo/ckpts/' + relative.as_posix(),
                      'object_key': 'assets/hymotion/ckpts/' + relative.as_posix(),
                      'repo': item['repo'], 'revision': item['revision'],
                      'source_url': f"https://huggingface.co/{item['repo']}/resolve/{item['revision']}/{quote(remote_path, safe='/')}",
                      'size_bytes': item['size_bytes'], 'sha256': item['sha256']})
        print(('SHA-256 verified: ' if verify_hashes else 'Size verified: ') + relative.as_posix(), flush=True)
    return {'source_manifest': 'official-model-manifest.json', 'source_verified_at': original.get('verified_at'),
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'local_size_verified': True, 'local_sha256_reverified': verify_hashes,
            'file_count': len(files), 'total_bytes': sum(item['size_bytes'] for item in files),
            'uploaded': False, 'files': files}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('assets-manifest.json'))
    parser.add_argument('--verify-hashes', action='store_true')
    args = parser.parse_args()
    manifest = prepare(args.backend_root.resolve(), args.verify_hashes)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"Verified transfer plan: {manifest['file_count']} files, {manifest['total_bytes']} bytes. Nothing uploaded.")
