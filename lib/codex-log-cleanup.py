#!/usr/bin/env python3
"""
Codex Log Cleanup - Prune old logs under ~/.codexlogs
Default retention: 90 days. Use --days -1 or --days false to retain forever.
"""

import os
import sys
import argparse
import pathlib
import time
from typing import List


def iter_log_files(base: pathlib.Path) -> List[pathlib.Path]:
    exts = {".jsonl", ".raw.txt", ".meta.json", ".ttylog"}
    files: List[pathlib.Path] = []
    if not base.exists():
        return files
    for p in base.rglob("*"):
        if p.is_file() and any(str(p).endswith(ext) for ext in exts):
            files.append(p)
    return files


def remove_empty_dirs(base: pathlib.Path) -> int:
    removed = 0
    # Walk bottom-up to remove empty dirs
    for root, dirs, _ in os.walk(base, topdown=False):
        for d in dirs:
            dp = pathlib.Path(root) / d
            try:
                next(dp.iterdir())
            except StopIteration:
                try:
                    dp.rmdir()
                    removed += 1
                except Exception:
                    pass
            except Exception:
                pass
    return removed


def main():
    parser = argparse.ArgumentParser(description="Prune old Codex logs")
    parser.add_argument("--days", type=str, default="90", help="Retention in days (default 90). Use -1 or false to retain forever.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without removing")
    parser.add_argument("--base", default=str(pathlib.Path.home() / ".codexlogs"), help="Base logs directory (default ~/.codexlogs)")

    args = parser.parse_args()

    # Interpret days
    days_raw = args.days.strip().lower()
    if days_raw in {"-1", "false", "infinite", "none"}:
        print("Cleanup disabled: retention set to forever")
        return
    try:
        days = int(days_raw)
    except ValueError:
        print(f"Invalid --days value: {args.days}", file=sys.stderr)
        sys.exit(2)
    if days < 0:
        print("Cleanup disabled: retention set to forever")
        return

    base = pathlib.Path(args.base).expanduser()
    cutoff = time.time() - days * 86400
    print(f"Pruning logs under {base} older than {days} days")

    files = iter_log_files(base)
    if not files:
        print("No log files found")
        return

    to_delete: List[pathlib.Path] = []
    for f in files:
        try:
            if f.stat().st_mtime < cutoff:
                to_delete.append(f)
        except FileNotFoundError:
            continue

    print(f"Found {len(files)} files; {len(to_delete)} eligible for deletion")

    if args.dry_run:
        for p in to_delete[:50]:
            print(f"DRY RUN: would delete {p}")
        if len(to_delete) > 50:
            print(f"... and {len(to_delete) - 50} more")
        return

    removed = 0
    for p in to_delete:
        try:
            p.unlink()
            removed += 1
        except Exception as e:
            print(f"Failed to remove {p}: {e}", file=sys.stderr)

    dirs_removed = remove_empty_dirs(base)
    print(f"Removed {removed} files and {dirs_removed} empty directories")


if __name__ == "__main__":
    main()

