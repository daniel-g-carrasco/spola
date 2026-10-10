"""Entry point: `spola <command>` is the command line, anything else the app."""

import sys

from . import i18n


def main() -> int:
    i18n.setup()
    from .cli import COMMANDS
    if len(sys.argv) > 1 and sys.argv[1] in COMMANDS + ("-h", "--help", "--version"):
        from .cli import main as cli_main
        return cli_main(sys.argv[1:])
    from .app import main as app_main
    return app_main(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
