"""One-shot cleanup tool for the Hard Drive Cleaner destination folder.

Earlier builds of the cleaner walked into Windows system folders like
$RECYCLE.BIN and System Volume Information, and moved their contents into
<dest>/misc/. This script finds and removes that pollution, plus common OS
junk files (desktop.ini, Thumbs.db, .DS_Store) scattered through the dest.

Safe by default — runs in dry-run mode and only shows what would be removed.
Pass --apply to actually delete.

Usage:
    python cleanup_destination.py "D:\\Sorted"            (preview)
    python cleanup_destination.py "D:\\Sorted" --apply    (delete for real)
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

from harddisk_cleaner import is_system_folder_name

# OS-generated nuisance files Windows / macOS scatter through any folder.
JUNK_FILES = {"desktop.ini", "thumbs.db", ".ds_store"}


def find_targets(dest_root: str) -> tuple[list[str], list[str]]:
    """Walk dest_root and return (system_dirs_to_remove, junk_files_to_remove)."""
    dirs_to_remove: list[str] = []
    files_to_remove: list[str] = []
    for root_dir, dirs, files in os.walk(dest_root):
        for d in list(dirs):
            if is_system_folder_name(d):
                dirs_to_remove.append(os.path.join(root_dir, d))
                dirs.remove(d)  # don't descend — we'll rmtree it
        for f in files:
            if f.lower() in JUNK_FILES:
                files_to_remove.append(os.path.join(root_dir, f))
    return dirs_to_remove, files_to_remove


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove Windows system-folder remnants and OS junk files "
                    "from a Hard Drive Cleaner destination.",
    )
    parser.add_argument("dest", help="path to the destination folder")
    parser.add_argument(
        "--apply", action="store_true",
        help="actually delete (default is dry-run preview)",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.dest):
        print(f"Not a directory: {args.dest}", file=sys.stderr)
        return 2

    dirs_to_remove, files_to_remove = find_targets(args.dest)

    if not dirs_to_remove and not files_to_remove:
        print("Nothing to clean — destination is tidy.")
        return 0

    print(
        f"Found {len(dirs_to_remove)} system folder(s) and "
        f"{len(files_to_remove)} junk file(s):"
    )
    for d in dirs_to_remove:
        print(f"  DIR  {d}")
    for f in files_to_remove:
        print(f"  FILE {f}")

    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply to actually delete.")
        return 0

    print()
    removed_dirs = removed_files = failed = 0
    for d in dirs_to_remove:
        try:
            shutil.rmtree(d)
            removed_dirs += 1
            print(f"removed dir  {d}")
        except OSError as exc:
            failed += 1
            print(f"FAILED dir   {d}: {exc}")
    for f in files_to_remove:
        try:
            os.remove(f)
            removed_files += 1
            print(f"removed file {f}")
        except OSError as exc:
            failed += 1
            print(f"FAILED file  {f}: {exc}")

    print()
    print(f"Done. Removed {removed_dirs} dirs, {removed_files} files. "
          f"Failures: {failed}.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
