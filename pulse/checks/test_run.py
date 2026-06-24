"""Compatibility wrapper for ``pulse.checks.test_run``."""

from heart.checks.test_run import *  # noqa: F401,F403

if __name__ == "__main__":
    import sys
    from heart.checks.test_run import main

    sys.exit(main(sys.argv))
