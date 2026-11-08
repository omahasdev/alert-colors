"""PyInstaller entry point. The exe is the dedicated process that Armoury Crate
registers in its Aura In-Game Priority list (ProcessList.ini)."""

import sys

from alert_colors.cli import main

sys.exit(main())
