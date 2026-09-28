#!/usr/bin/env bash
# Every test: shell syntax, the Python core, the Omarchy widget's model,
# and backup-sync end to end against throwaway directories.
set -euo pipefail
cd "$(dirname "$0")/.."
bash -n install.sh build.sh uninstall.sh scripts/backup-sync scripts/mirror-backup
python3 -m unittest discover -s tests
if command -v node >/dev/null; then
    node tests/test_omarchy_model.mjs
else
    echo "node not found — skipping tests/test_omarchy_model.mjs" >&2
fi
tests/test_backup_sync.sh
