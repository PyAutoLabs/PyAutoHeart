"""Compatibility wrapper for ``pulse.noise``."""

from heart.noise import *  # noqa: F401,F403

if __name__ == "__main__":
    import sys
    from heart.noise import main

    sys.exit(main())
