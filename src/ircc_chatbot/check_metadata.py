"""Check the metadata files before a deploy.

    PYTHONPATH=src python3 -m ircc_chatbot.check_metadata
    PYTHONPATH=src python3 -m ircc_chatbot.check_metadata --metadata-dir /path/to/metadata

What: runs exactly the checks the application runs at startup, without starting it.
Exit code: 0 if the metadata is valid, 1 if not (the error names the file and field).

How: calls load_metadata(), which reads the files and runs metadata_checks.py,
then prints the file versions and counts.

Why: the service runs 24/7 and refuses to start on bad metadata. Running this
first (by hand, or as a step in the deploy pipeline) catches a bad file before
it can cause an outage.
"""

from __future__ import annotations

import argparse
import sys

from .errors import MetadataError
from .metadata_loader import DEFAULT_METADATA_DIR, load_metadata


def main(argv: list[str] | None = None) -> int:
    """Load and check the metadata; print OK with versions, or the error. Return the exit code."""
    parser = argparse.ArgumentParser(description="Check the metadata files without starting the chatbot.")
    parser.add_argument("--metadata-dir", help=f"Folder with the metadata files (default: {DEFAULT_METADATA_DIR}).")
    args = parser.parse_args(argv)

    try:
        md = load_metadata(args.metadata_dir)
    except MetadataError as exc:
        print(f"Metadata check FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"Metadata OK ({args.metadata_dir or DEFAULT_METADATA_DIR})")
    for name, version in md.versions.items():
        print(f"  {name:<18} version {version}")
    print(f"  {len(md.stereotypes)} stereotypes, {len(md.operators)} operators, {len(md.query_types)} query types")
    return 0


if __name__ == "__main__":
    sys.exit(main())
