"""Entry point: `spola <command>` is the command line, anything else the app."""

import os
import sys

from . import i18n

_stacks = None


def main() -> int:
    global _stacks
    if os.environ.get("SPOLA_DEV_STACKS") and not getattr(sys, "frozen", False):
        # developer aid: every thread's stack written to that file every
        # 30 s, to see where a Spola that seems stuck is (never the built app)
        import faulthandler
        _stacks = open(os.environ["SPOLA_DEV_STACKS"], "w")
        faulthandler.dump_traceback_later(30, repeat=True, file=_stacks)
    i18n.setup()
    from .cli import COMMANDS
    if len(sys.argv) > 1 and sys.argv[1] in COMMANDS + ("-h", "--help", "--version"):
        from .cli import main as cli_main
        return cli_main(sys.argv[1:])
    from .app import main as app_main
    return app_main(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
