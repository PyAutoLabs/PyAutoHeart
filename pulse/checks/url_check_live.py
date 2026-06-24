"""Compatibility wrapper for ``pulse.checks.url_check_live``."""

from heart.checks.url_check_live import *  # noqa: F401,F403

if __name__ == "__main__":
    import sys
    from heart.checks.url_check_live import main

    sys.exit(main(sys.argv[1:]))
