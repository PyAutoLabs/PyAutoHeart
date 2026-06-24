"""Compatibility wrapper for ``pulse.readiness``."""

from heart.readiness import *  # noqa: F401,F403

if __name__ == "__main__":
    import sys
    from heart.readiness import main

    sys.exit(main())
