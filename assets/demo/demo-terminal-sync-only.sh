#!/bin/zsh
set -euo pipefail

ROOT_DIR="/Users/longbiao/Projects/feishu-backup"
DEMO_STATE_ROOT="${ROOT_DIR}/.demo-state"
DEMO_DOCS_ROOT="${HOME}/Documents/feishu-backup-demo"

printf '\033c'
cd "${ROOT_DIR}"

echo "Feishu Backup"
echo "Incremental sync with document-level resume."
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
echo "GitHub: https://github.com/longbiaochen/feishu-backup"
echo "Install: pipx install git+https://github.com/longbiaochen/feishu-backup.git"
