#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEMO_RUN_SCRIPT="${ROOT_DIR}/assets/demo/demo-terminal-run.sh"

osascript <<OSA
tell application "Terminal"
    activate
    do script quoted form of POSIX path of "${DEMO_RUN_SCRIPT}"
end tell
OSA
