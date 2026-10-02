#!/usr/bin/env bash
# Every test: shell syntax, the Python core, the Omarchy widget's model,
# and backup-sync end to end against throwaway directories.
set -euo pipefail
cd "$(dirname "$0")/.."
# As scripts/mirror-backup: the system interpreter, which has PyGObject.
PYTHON="${MIRROR_BACKUP_PYTHON:-/usr/bin/python3}"
bash -n install.sh build.sh uninstall.sh scripts/backup-sync scripts/mirror-backup scripts/vendor-adw-omarchy.sh
"$PYTHON" -m unittest discover -s tests
if command -v node >/dev/null; then
    node tests/test_omarchy_model.mjs
else
    echo "node not found — skipping tests/test_omarchy_model.mjs" >&2
fi
tests/test_backup_sync.sh
