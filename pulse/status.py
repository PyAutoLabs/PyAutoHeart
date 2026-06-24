"""Compatibility wrapper for ``pulse.status``."""

from heart.status import *  # noqa: F401,F403

if __name__ == "__main__":
    import sys
    from heart.status import main

    sys.exit(main())
