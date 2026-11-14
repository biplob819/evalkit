"""Command-line entry point.

W0.1 ships only `--version` so the package installs and runs. The full Typer CLI
arrives in node B2.2 and the onboarding commands in B2.4.
"""

from __future__ import annotations

import argparse
import sys

from evalkit import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evalkit",
        description="Evaluate your own task across Claude and OpenAI models.",
    )
    parser.add_argument("--version", action="version", version=f"evalkit {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
