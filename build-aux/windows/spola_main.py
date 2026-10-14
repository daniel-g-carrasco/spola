"""Entry point of the Windows build (PyInstaller freezes this file)."""

import sys

from spola.__main__ import main

sys.exit(main())
