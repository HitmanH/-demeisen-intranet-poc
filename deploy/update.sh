#!/usr/bin/env bash
# Data-preserving updater for the live intranet. Run ON THE SERVER as root:
#   curl -fsSL https://raw.githubusercontent.com/HitmanH/-demeisen-intranet-poc/main/deploy/update.sh | sudo bash
# Replaces code only (*.py, templates/, static/, data/assets/). Never touches
# data/intranet.db, data/generated or data/uploads. Backs up first, rolls back if the app fails to start.
set -euo pipefail
REF="${1:-main}"
APP=/opt/intranet
URL="https://codeload.github.com/HitmanH/-demeisen-intranet-poc/tar.gz/refs/heads/$REF"
TS=$(date +%Y%m%d-%H%M%S)
BK=/opt/intranet-backups/$TS
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$BK"
echo "== backup -> $BK"
tar -C "$APP" --exclude=./.venv --exclude=./data/intranet.db --exclude=./data/generated --exclude=./data/uploads -czf "$BK/code.tgz" .
[ -f "$APP/data/intranet.db" ] && sqlite3 "$APP/data/intranet.db" ".backup '$BK/intranet.db'" 2>/dev/null \
  || cp -a "$APP/data/intranet.db" "$BK/intranet.db"

echo "== download $REF"
curl -fsSL "$URL" | tar -xz -C "$WORK" --strip-components=1

echo "== install code (data/ preserved)"
install -m 644 -o intranet -g intranet "$WORK"/*.py "$APP"/
for d in templates static data/assets; do
  mkdir -p "$APP/$d"
  cp -a "$WORK/$d/." "$APP/$d/"
done
chown -R intranet:intranet "$APP/templates" "$APP/static" "$APP/data/assets"
mkdir -p "$APP/deploy" && cp -a "$WORK/deploy/." "$APP/deploy/"

echo "== restart"
systemctl restart intranet
sleep 3
if curl -fsS -o /dev/null http://127.0.0.1/login; then
  echo "== OK: deployed $REF ($TS)"
  ls -1dt /opt/intranet-backups/* | tail -n +11 | xargs -r rm -rf   # keep last 10 backups
else
  echo "!! app failed to start - rolling back code"
  tar -C "$APP" -xzf "$BK/code.tgz"
  systemctl restart intranet
  exit 1
fi
