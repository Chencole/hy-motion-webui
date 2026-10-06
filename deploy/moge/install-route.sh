#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" -eq 0
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
target=/www/server/panel/vhost/nginx/extension/mogestudio.com/hymotion.conf
site=/www/server/panel/vhost/nginx/mogestudio.com.conf
grep -Fq 'include /www/server/panel/vhost/nginx/extension/mogestudio.com/*.conf;' "$site"
python3.11 "$script_dir/healthcheck.py"
nginx -t
backup="/var/backups/hymotion-webui/nginx-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$backup"
cp -a "$site" "$backup/mogestudio.com.conf.reference"
existed=0
if test -f "$target"; then
  existed=1
  cp -a "$target" "$backup/hymotion.conf"
fi
rollback() {
  if test "$existed" -eq 1; then
    cp -a "$backup/hymotion.conf" "$target"
  else
    rm -f -- "$target"
  fi
  nginx -t && systemctl reload nginx
}
install -m 0644 "$script_dir/nginx-motion.conf" "$target"
if ! nginx -t || ! systemctl reload nginx; then
  rollback
  exit 1
fi
if ! python3.11 "$script_dir/healthcheck.py" https://mogestudio.com/motion/api/health; then
  rollback
  exit 1
fi
printf 'Published https://mogestudio.com/motion/; backup: %s\n' "$backup"
