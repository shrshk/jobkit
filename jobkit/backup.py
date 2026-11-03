"""job-backup: an app's folders to an off-site rclone remote, then a check that the copy matches. Each app backs up
its own data with it (no app has Backblaze code of its own); run it under job-run so the run is recorded:

    job-run --db "$DB" --app photo-app --describe "Photos to Backblaze (encrypted)" --schedule "daily 3:00" \\
        photos-offsite -- job-backup --to backup:photo-app "/Volumes/Drive/Photos"

The remote is whatever rclone config says (RCLONE_CONFIG or --config): for Backblaze, a crypt remote over a B2
bucket, so contents and names are encrypted before they leave the machine. Several folders go to subfolders named
after each folder. Safeguards:
- A missing or empty folder (drive unplugged) is never synced: that would delete the off-site copy. The run fails.
- A run that finds the last one still going (the first upload takes days) leaves it to finish.
- After the sync, `rclone check --one-way --size-only` compares the listings (cheap); --cryptcheck reads every
  local file and compares checksums through the crypt remote (thorough, slow).
The last line is the summary the jobs page shows: "2 folders, 12,345 files, 37.1 GB, copy matches".
"""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

EXCLUDES = ["._*", ".DS_Store", ".Spotlight-V100/**", ".fseventsd/**", ".TemporaryItems/**", "$RECYCLE.BIN/**"]
LOCK_DIR = Path.home() / ".cache/jobkit"


def log(text: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}", flush=True)


def rclone(rclone_cmd: str, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run([rclone_cmd, *args], check=False, capture_output=capture, text=True)


def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1000
    return f"{size:.1f} TB"


def backup(
    folders: list[str],
    to: str,
    excludes: tuple[str, ...] | list[str] = (),
    cryptcheck: bool = False,
    dry_run: bool = False,
    rclone_cmd: str = "rclone",
) -> tuple[int, str]:
    """Syncs each folder to `to` (one folder: `to` itself; several: `to/<folder name>`), then checks the copy.
    Returns the exit code and the one-line summary. In an app: `with jobs.run(name) as run:` then
    `code, run.summary = backup(...)`, raising when code is not 0."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_DIR / f"backup-{hashlib.sha256(to.encode()).hexdigest()[:12]}.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"skip: the last backup to {to} is still running")
            return 0, "skipped: the last backup is still running"
        return sync_all(folders, to, excludes, cryptcheck, dry_run, rclone_cmd)


def counted(rclone_cmd: str, src: Path, filters: list[str]) -> dict | None:
    """What rclone itself would sync from src ({"count", "bytes"}), with the same filters; None when it cannot say."""
    done = rclone(rclone_cmd, "size", str(src), "--json", *filters, capture=True)
    if done.returncode:
        return None
    try:
        return json.loads(done.stdout)
    except ValueError:
        return None


def sync_all(folders, to, excludes, cryptcheck, dry_run, rclone_cmd) -> tuple[int, str]:
    filters = [a for e in [*EXCLUDES, *excludes] for a in ("--exclude", e)]
    sources = [Path(f).resolve() for f in folders]  # absolute: a folder named "-x" is never read as a flag
    names = [p.name for p in sources]
    if to.startswith("-") or not to:
        return 1, f"not a remote: {to!r}"
    if len(sources) > 1 and (len(set(names)) < len(names) or "" in names):
        # Several folders go to to/<name>: two with one name would delete each other's copy on every run.
        return 1, "two folders share a name (or one is /): give folders with different names"
    status, files, size, problems = 0, 0, 0, []
    for src in sources:
        dst = to if len(sources) == 1 else f"{to.rstrip('/')}/{src.name}"
        # Never sync nothing: that deletes the off-site copy. A drive that is not mounted can leave its mount point
        # behind with only Finder's files in it (.DS_Store, ._*), which the filters skip; so count what rclone
        # itself would send, with the same filters.
        found = counted(rclone_cmd, src, filters) if src.is_dir() else None
        if not found or not found.get("count"):
            log(f"{src} is missing or empty; not synced (the off-site copy stays as it was)")
            problems.append(f"{src.name} missing")
            status = 1
            continue
        log(f"sync {src} -> {dst}")
        extra = ["--dry-run"] if dry_run else []
        if rclone(
            rclone_cmd,
            "sync",
            str(src),
            dst,
            *filters,
            *extra,
            "--transfers",
            "4",
            "--fast-list",
            "--stats",
            "10m",
            "--stats-one-line",
        ).returncode:
            problems.append(f"{src.name} sync failed")
            status = 1
            continue
        if not dry_run:
            check = ["cryptcheck", str(src), dst] if cryptcheck else ["check", str(src), dst, "--size-only"]
            if rclone(rclone_cmd, *check, "--one-way", *filters).returncode:
                problems.append(f"{src.name} copy differs")
                status = 1
        files, size = files + found["count"], size + found.get("bytes", 0)

    result = "; ".join(problems) or ("dry run" if dry_run else "copy matches")
    noun = "folder" if len(folders) == 1 else "folders"
    return status, f"{len(folders)} {noun}, {files:,} files, {human(size)}, {result}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="job-backup", description="Folders to an off-site rclone remote, then a check.")
    p.add_argument("folders", nargs="+")
    p.add_argument("--to", required=True, help="rclone destination, e.g. backup:photo-app")
    p.add_argument("--config", help="rclone config file (default: RCLONE_CONFIG or rclone's own)")
    p.add_argument("--exclude", action="append", default=[], help="an extra rclone exclude pattern (repeatable)")
    p.add_argument("--cryptcheck", action="store_true", help="compare checksums, not only sizes (slow)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if args.config:
        os.environ["RCLONE_CONFIG"] = args.config
    code, summary = backup(args.folders, args.to, args.exclude, args.cryptcheck, args.dry_run)
    print(summary, flush=True)  # the last line: job-run makes it the run's summary
    return code


if __name__ == "__main__":
    sys.exit(main())
