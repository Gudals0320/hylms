"""Compatibility facade and executable entry point for the HY-LMS snapshot CLI."""

from hylms import *  # noqa: F401,F403 - preserve the original public module surface


if __name__ == "__main__":
    raise SystemExit(main())
