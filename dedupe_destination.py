"""One-shot dedup of an existing Hard Drive Cleaner destination folder.

Walks every file under the destination, computes MD5 by content, groups
identical files, keeps one canonical copy per hash, and moves the rest
into <dest>/duplicates/. Finally writes the state file (.hdc_hash.json)
so future runs of the main app pick up the cleaned state.

Necessary because earlier builds of the tool didn't persist the hash map
across stop/restart, so users who stopped and resumed runs may have
content-duplicate copies sitting in their year/month folders.

Usage:
    python dedupe_destination.py "D:\\Sorted"            (dry-run preview)
    python dedupe_destination.py "D:\\Sorted" --apply    (commit changes)

The chosen "winner" for each hash group is the path with the simplest
filename (no _1, _2, ... collision suffix), tie-broken alphabetically.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys

# Inlined copies of the helpers/constants we need from harddisk_cleaner.
# Kept in sync deliberately — this script is meant to run standalone without
# needing the main app's GUI deps (tkinter / PIL).
HASH_STATE_FILENAME = ".hdc_hash.json"
CHUNK_SIZE = 64 * 1024


def md5_of_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_collision(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{stem}_{i}{ext}"):
        i += 1
    return f"{stem}_{i}{ext}"


DUPES_DIRNAME = "duplicates"
COLLISION_SUFFIX_RE = re.compile(r"_(\d+)(\.[^.]+)?$")


def _has_collision_suffix(name: str) -> bool:
    """True if filename ends in `_N` or `_N.ext` — likely a collision-renamed copy."""
    return bool(COLLISION_SUFFIX_RE.search(name))


def _winner_key(path: str) -> tuple[int, str]:
    """Sort key: prefer no-suffix names, then alphabetical."""
    name = os.path.basename(path)
    return (1 if _has_collision_suffix(name) else 0, path)


def scan(dest_root: str) -> dict[str, list[str]]:
    """Return MD5 -> list of file paths inside dest_root (skipping state file
    and the duplicates folder, which is already a dump)."""
    dupes_root = os.path.join(dest_root, DUPES_DIRNAME)
    dupes_root_norm = os.path.normcase(os.path.abspath(dupes_root))

    hash_to_paths: dict[str, list[str]] = {}
    for root_dir, dirs, files in os.walk(dest_root):
        # Skip the duplicates folder itself — its contents are already
        # quarantined and don't need to be re-evaluated.
        if os.path.normcase(os.path.abspath(root_dir)) == dupes_root_norm:
            dirs[:] = []
            continue
        for name in files:
            if name == HASH_STATE_FILENAME:
                continue
            path = os.path.join(root_dir, name)
            try:
                digest = md5_of_file(path)
            except OSError as exc:
                print(f"  [ERROR] {path}: {exc}", file=sys.stderr)
                continue
            hash_to_paths.setdefault(digest, []).append(path)
    return hash_to_paths


def plan_moves(
    hash_to_paths: dict[str, list[str]], dest_root: str,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Return (winners_by_hash, [(src, dest), ...] moves to perform)."""
    winners: dict[str, str] = {}
    moves: list[tuple[str, str]] = []
    dupes_dir = os.path.join(dest_root, DUPES_DIRNAME)
    for digest, paths in hash_to_paths.items():
        if len(paths) == 1:
            winners[digest] = paths[0]
            continue
        paths_sorted = sorted(paths, key=_winner_key)
        winner = paths_sorted[0]
        winners[digest] = winner
        for loser in paths_sorted[1:]:
            target = resolve_collision(os.path.join(dupes_dir, os.path.basename(loser)))
            moves.append((loser, target))
    return winners, moves


def write_state(dest_root: str, winners: dict[str, str]) -> None:
    state_path = os.path.join(dest_root, HASH_STATE_FILENAME)
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"hashes": winners}, f)
    os.replace(tmp, state_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dest", help="destination folder to dedupe")
    parser.add_argument(
        "--apply", action="store_true",
        help="actually move duplicates and write state (default: dry-run preview)",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.dest):
        print(f"Not a directory: {args.dest}", file=sys.stderr)
        return 2

    print(f"Scanning {args.dest} ... this re-hashes every file, please wait.")
    hash_to_paths = scan(args.dest)

    total_files = sum(len(p) for p in hash_to_paths.values())
    dupe_groups = [paths for paths in hash_to_paths.values() if len(paths) > 1]
    print(
        f"Scanned {total_files} files. Found {len(dupe_groups)} "
        f"duplicate group(s) holding {sum(len(g) - 1 for g in dupe_groups)} "
        f"extra cop(y/ies)."
    )

    if not dupe_groups:
        if args.apply:
            winners, _ = plan_moves(hash_to_paths, args.dest)
            write_state(args.dest, winners)
            print(f"Wrote fresh {HASH_STATE_FILENAME} ({len(winners)} hashes).")
        else:
            print("Nothing to dedupe. Re-run with --apply to also write a fresh state file.")
        return 0

    winners, moves = plan_moves(hash_to_paths, args.dest)

    print()
    print(f"Plan ({'APPLY' if args.apply else 'dry-run'}):")
    for digest, paths in hash_to_paths.items():
        if len(paths) <= 1:
            continue
        win = winners[digest]
        print(f"  KEEP   {win}")
        for loser in paths:
            if loser == win:
                continue
            print(f"  DUPE   {loser}")

    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply to move duplicates "
              "and write the state file.")
        return 0

    print()
    moved = failed = 0
    for src, dst in moves:
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            moved += 1
            print(f"  moved  {src}")
        except OSError as exc:
            failed += 1
            print(f"  FAILED {src}: {exc}", file=sys.stderr)

    write_state(args.dest, winners)
    print()
    print(f"Done. Moved {moved} duplicate(s) to {DUPES_DIRNAME}/. "
          f"Failures: {failed}. State file refreshed ({len(winners)} hashes).")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
