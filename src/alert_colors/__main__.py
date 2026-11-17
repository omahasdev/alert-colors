import sys

from . import hardening

hardening.apply()  # before anything else: the app can run as admin

from .cli import main  # noqa: E402  after hardening

sys.exit(main())
