"""Compatibility wrapper for ``pulse.checks.script_timing``."""

from heart.checks.script_timing import *  # noqa: F401,F403

if __name__ == "__main__":
    import sys
    from heart.checks.script_timing import main

    sys.exit(main(sys.argv))
