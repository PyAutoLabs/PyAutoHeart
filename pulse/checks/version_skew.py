"""Compatibility wrapper for ``pulse.checks.version_skew``."""

from heart.checks.version_skew import *  # noqa: F401,F403

if __name__ == "__main__":
    import sys
    from heart.checks.version_skew import main

    sys.exit(main(sys.argv))
