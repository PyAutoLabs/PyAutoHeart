"""Compatibility wrapper for ``pulse.fix``."""

from heart.fix import *  # noqa: F401,F403

if __name__ == "__main__":
    import sys
    from heart.fix import main

    sys.exit(main(sys.argv))
