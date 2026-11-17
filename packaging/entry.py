"""PyInstaller entry point. The exe is the dedicated process that Armoury Crate
registers in its Aura In-Game Priority list (ProcessList.ini)."""

import sys

from alert_colors import hardening

hardening.apply()  # before anything else: the app runs as admin

from alert_colors.cli import main  # noqa: E402  after hardening

sys.exit(main())
