"""Command-line entry point, run as `quiff` or `python -m quiff`."""

from quiff import __version__


def main() -> None:
    print(f"quiff {__version__}")
