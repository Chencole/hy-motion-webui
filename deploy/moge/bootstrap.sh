#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" -eq 0
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# uv owns the shared Python installation; neither system nor panel Python changes.
if ! command -v uv >/dev/null 2>&1; then
  for candidate in /root/.local/bin/uv /root/.cargo/bin/uv /usr/local/bin/uv; do
    if test -x "$candidate"; then
      printf 'Existing uv outside PATH: %s. Configure its normal PATH first.\n' "$candidate" >&2
      exit 1
    fi
  done
  install -d -m 0755 /var/cache/hymotion-webui
  curl --fail --location --silent --show-error --connect-timeout 15 --max-time 180 \
    https://astral.sh/uv/install.sh -o /var/cache/hymotion-webui/uv-install.sh
  env UV_INSTALL_DIR=/usr/local/bin sh /var/cache/hymotion-webui/uv-install.sh
fi

profile=/etc/profile.d/uv-managed-python.sh
profile_content='export UV_PYTHON_INSTALL_DIR=/opt/uv/python
export UV_PYTHON_BIN_DIR=/usr/local/bin'
if test -e "$profile" && ! cmp -s "$profile" <(printf '%s\n' "$profile_content"); then
  printf 'Refusing to overwrite existing %s\n' "$profile" >&2
  exit 1
fi
printf '%s\n' "$profile_content" > "$profile"
chmod 0644 "$profile"
source "$profile"
install -d -m 0755 /opt/uv/python
uv python install 3.11

if ! id hymotion-webui >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/hymotion-webui --shell /sbin/nologin hymotion-webui
fi
install -d -m 0755 /opt/hymotion-webui /opt/hymotion-webui/releases /opt/hymotion-webui/deploy
install -d -m 0700 /etc/hymotion-webui /var/backups/hymotion-webui
install -d -m 0750 -o hymotion-webui -g hymotion-webui /var/lib/hymotion-webui
for file in "$script_dir"/*; do
  test -f "$file" || continue
  destination="/opt/hymotion-webui/deploy/$(basename -- "$file")"
  test "$file" -ef "$destination" && continue
  install -m 0644 "$file" "$destination"
done
if test -e /etc/systemd/system/hymotion-webui.service; then
  cp -a /etc/systemd/system/hymotion-webui.service "/var/backups/hymotion-webui/service-$(date -u +%Y%m%dT%H%M%SZ).bak"
fi
install -m 0644 "$script_dir/hymotion-webui.service" /etc/systemd/system/hymotion-webui.service
systemctl daemon-reload

# Read a separate, root-only login file; never print the password or digest.
if test -s /etc/hymotion-webui/login.txt && ! test -e /etc/hymotion-webui/app.env; then
  python3.11 "$script_dir/configure-login.py"
fi
bash -lc 'command -v uv; uv --version; command -v python3.11; python3.11 --version; uv python dir'
printf 'Runtime and service prepared. Application is not started or enabled.\n'
