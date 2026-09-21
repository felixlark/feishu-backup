#!/bin/zsh
set -euo pipefail

ROOT_DIR="/Users/longbiao/Projects/feishu-backup"
DEMO_STATE_ROOT="${ROOT_DIR}/.demo-state"
DEMO_DOCS_ROOT="${HOME}/Documents/feishu-backup-demo"

printf '\033c'
cd "${ROOT_DIR}"

echo "Feishu Backup: back up Feishu knowledge bases to local disk."
echo
echo "Built on top of feishu-docx."
echo
sed -n '1,20p' README.md
sleep 3

echo
echo '$ feishu-backup authorize'
(
  for _ in {1..12}; do
    sleep 2
    /usr/bin/osascript -e 'tell application "Safari" to activate' \
      -e 'tell application "Safari" to do JavaScript "Array.from(document.querySelectorAll(\"button\")).find(b => b.innerText.includes(\"授权\"))?.click()" in current tab of front window' \
      >/dev/null 2>&1 || true
  done
) &
STATE_ROOT="${DEMO_STATE_ROOT}" ./scripts/backup authorize
sleep 2
/usr/bin/osascript -e 'tell application "Terminal" to activate'
sleep 1

echo
echo '$ feishu-backup sync'
STATE_ROOT="${DEMO_STATE_ROOT}" ./scripts/backup sync
sleep 2

echo
echo '$ find ~/Documents/feishu-backup-demo -maxdepth 3'
find "${DEMO_DOCS_ROOT}" -maxdepth 3 | sed "s#${HOME}#~#"
echo
latest_summary="$(ls -t "${DEMO_STATE_ROOT}/logs/"*.summary.txt | head -n 1)"
echo '$ cat latest summary'
cat "${latest_summary}"
echo
echo "GitHub: https://github.com/felixlark/feishu-backup"
echo "Install: pipx install git+https://github.com/felixlark/feishu-backup.git"
