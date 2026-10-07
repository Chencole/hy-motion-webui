#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" -eq 0
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
target=/www/server/panel/vhost/nginx/extension/120.24.92.122/hymotion.conf
site=/www/server/panel/vhost/nginx/120.24.92.122.conf
legacy=/www/server/panel/vhost/nginx/extension/mogestudio.com/hymotion.conf
grep -Fq 'include /www/server/panel/vhost/nginx/extension/120.24.92.122/*.conf;' "$site"
# Remove only this application's exact previous route; never overwrite a changed site include.
if test -f "$legacy" && ! cmp -s <(tail -n +2 "$legacy") <(tail -n +2 "$script_dir/nginx-motion.conf"); then
  printf 'Previous HY-Motion domain route has changed; review %s before moving it.\n' "$legacy" >&2
  exit 1
fi
python3.11 "$script_dir/healthcheck.py"
nginx -t
backup="/var/backups/hymotion-webui/nginx-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$backup"
cp -a "$site" "$backup/120.24.92.122.conf.reference"
existed=0
legacy_existed=0
if test -f "$target"; then
  existed=1
  cp -a "$target" "$backup/hymotion.conf"
fi
if test -f "$legacy"; then
  legacy_existed=1
  cp -a "$legacy" "$backup/legacy-hymotion.conf"
fi
rollback() {
  if test "$existed" -eq 1; then
    cp -a "$backup/hymotion.conf" "$target"
  else
    rm -f -- "$target"
  fi
  if test "$legacy_existed" -eq 1; then
    cp -a "$backup/legacy-hymotion.conf" "$legacy"
  fi
  nginx -t && systemctl reload nginx
}
install -d -m 0755 "$(dirname -- "$target")"
install -m 0644 "$script_dir/nginx-motion.conf" "$target"
if test "$legacy_existed" -eq 1; then
  rm -f -- "$legacy"
fi
if ! nginx -t || ! systemctl reload nginx; then
  rollback
  exit 1
fi
if ! python3.11 "$script_dir/healthcheck.py" http://120.24.92.122/motion/api/health; then
  rollback
  exit 1
fi
printf 'Published http://120.24.92.122/motion/; backup: %s\n' "$backup"
