"""`python3 -m backup_monitor [command]` — the app, or a command-line command.

Commands (backup_monitor.cli) run without importing GTK, so the panel widgets
can call `mirror-backup status --json` cheaply and it works without a display.
"""

import sys

from backup_monitor import cli

if len(sys.argv) > 1 and (sys.argv[1] in cli.COMMANDS
                          or sys.argv[1] in ('--regenerate-units', '-h', '--help')):
    sys.exit(cli.main(sys.argv[1:]))

from backup_monitor.main import main  # noqa: E402  (GTK only on this path)

sys.exit(main())
