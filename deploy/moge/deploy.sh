#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" -eq 0
archive="$(readlink -f -- "${1:?Provide the release tar.gz}")"
expected_sha="${2:?Provide the archive SHA256}"
printf '%s  %s\n' "$expected_sha" "$archive" | sha256sum --check --status
source /etc/profile.d/uv-managed-python.sh
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
release="/opt/hymotion-webui/releases/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0755 "$release"
# Reject traversal and link members before extracting this explicit allowlist package.
python3.11 - "$archive" "$release" <<'PY'
import sys, tarfile
from pathlib import Path, PurePosixPath
archive, destination = sys.argv[1:]
with tarfile.open(archive, 'r:gz') as bundle:
    members = bundle.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
            raise SystemExit('Unsafe archive member')
    bundle.extractall(destination, members=members, filter='data')
assert (Path(destination) / 'app.py').is_file()
assert (Path(destination) / 'uv.lock').is_file()
PY
uv sync --project "$release" --frozen --no-dev --python 3.11
chmod -R a+rX "$release"
previous="$(readlink /opt/hymotion-webui/current || true)"
ln -sfn "$release" /opt/hymotion-webui/current.next
mv -Tf /opt/hymotion-webui/current.next /opt/hymotion-webui/current
if ! systemctl restart hymotion-webui || ! python3.11 "$script_dir/healthcheck.py"; then
  if test -n "$previous"; then
    ln -sfn "$previous" /opt/hymotion-webui/current.next
    mv -Tf /opt/hymotion-webui/current.next /opt/hymotion-webui/current
    systemctl restart hymotion-webui
  else
    systemctl stop hymotion-webui
    rm -f -- /opt/hymotion-webui/current
  fi
  exit 1
fi
systemctl enable hymotion-webui
printf 'Active release: %s\n' "$release"
