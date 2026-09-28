#!/bin/bash
# Launch Mirror Backup desktop app in development mode
cd "$(dirname "$0")"
PYTHONPATH="src:$PYTHONPATH" exec python3 -m backup_monitor.main "$@"
