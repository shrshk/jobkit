"""The off-site backup's rclone setup, shared by every app on this Mac: one encrypted remote named "backup" in an
owner-only config file, made once from the first app's Backup page and found by the others (each backs up into its
own folder, backup:<app>). Used with job-backup:

    if not offsite.status()["configured"]:
        code = offsite.setup_b2(key_id, key, bucket)  # show the recovery code once
    job-backup --config ~/.config/rclone/backup.conf --to backup:<app> FOLDER

The Backblaze key goes only into the config file (mode 600) and to Backblaze: it is checked with rclone through
environment variables, never on a command line, and never logged. The encryption password is made here; without it
nobody can restore the backup, so the app shows it once as the recovery code and the person saves it.
"""

import configparser
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path

CONFIG = Path(os.environ.get("JOBKIT_BACKUP_CONFIG", Path.home() / ".config/rclone/backup.conf"))
REMOTE = "backup"
NAME = re.compile(r"[A-Za-z0-9._-]+")  # a B2 key ID or bucket name: nothing that could change the config or the path


class SetupError(Exception):
    """What went wrong, in words fit for the Backup page (never the key)."""


def status(path: Path = CONFIG) -> dict:
    """Whether this Mac has the off-site remote, and where it writes (no secrets): {"configured", "where"}."""
    conf = configparser.ConfigParser(interpolation=None)
    try:
        conf.read(path)
    except configparser.Error:
        return {"configured": False, "where": ""}
    if not conf.has_section(REMOTE):
        return {"configured": False, "where": ""}
    return {"configured": True, "where": conf.get(REMOTE, "remote", fallback="")}


def check(base: dict, bucket: str, rclone: str = "rclone") -> None:
    """Lists the bucket with these settings, passed to rclone as environment variables (so the key never shows in
    the process list). Raises SetupError when it cannot."""
    env = os.environ | {f"RCLONE_CONFIG_CHECK_{k.upper()}": str(v) for k, v in base.items()}
    try:
        done = subprocess.run(
            [rclone, "lsf", f"check:{bucket}", "--max-depth", "1"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SetupError(f"rclone did not run: {e}") from e
    if done.returncode:
        err = done.stderr.lower()
        if "401" in err or "unauthorized" in err or "bad_auth" in err:
            raise SetupError("Backblaze refused the key: check the key ID and the key.")
        if "404" in err or "not found" in err or "not exist" in err or "bad_bucket" in err:
            raise SetupError(f"No bucket named {bucket} for this key: check the name, and that the key may use it.")
        raise SetupError("Could not list the bucket with this key.")


def setup(base: dict, bucket: str, path: Path = CONFIG, rclone: str = "rclone", folder: str = "backups") -> str:
    """Checks the storage, then writes the config: section "store" (the storage itself) and "backup" (encryption
    over store:<bucket>/<folder>, "backups" unless given). Returns the recovery code (the encryption password). Refuses to replace an existing
    setup: its recovery code is what restores what is already uploaded."""
    # Any config there at all (even one this cannot read) may hold the password to what is already uploaded.
    if status(path)["configured"]:
        raise SetupError("This Mac already has an off-site backup set up; use it.")
    if path.exists():
        raise SetupError(f"{path} is there but has no backup in it: move it away first (it may hold a password).")
    values = [*map(str, base.values()), bucket, folder]
    if any(ch in v for v in values for ch in "\r\n\0") or any(not v.strip() for v in (bucket, folder)):
        raise SetupError("A value has a line break in it, or is empty: paste it again.")
    check(base, bucket, rclone)
    code = secrets.token_urlsafe(32)
    try:
        obscured = subprocess.run([rclone, "obscure", "-"], input=code, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as e:
        raise SetupError("rclone could not prepare the encryption password.") from e
    conf = configparser.ConfigParser(interpolation=None)
    conf["store"] = {k: str(v) for k, v in base.items()}
    conf[REMOTE] = {"type": "crypt", "remote": f"store:{bucket}/{folder}", "password": obscured.stdout.strip()}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".backup-", suffix=".tmp")  # new, owner-only (0600)
    try:
        with os.fdopen(fd, "w") as f:
            conf.write(f)
        os.link(tmp, path)  # fails if another setup got there first: never replaces one
    except FileExistsError as e:
        raise SetupError("This Mac already has an off-site backup set up; use it.") from e
    finally:
        os.unlink(tmp)
    return code


def setup_b2(
    key_id: str, key: str, bucket: str, path: Path = CONFIG, rclone: str = "rclone", folder: str = "backups"
) -> str:
    """Backblaze B2: an application key limited to one bucket (Read and Write). Deleted files stay as old versions
    for as long as the bucket's lifecycle rule keeps them (hard_delete off)."""
    key_id, key, bucket = key_id.strip(), key.strip(), bucket.strip()
    if not (key_id and key and bucket):
        raise SetupError("The key ID, the key and the bucket name are all needed.")
    if not (NAME.fullmatch(key_id) and NAME.fullmatch(bucket)) or not key.isprintable():
        raise SetupError("The key ID, the key or the bucket name has characters B2 never uses: paste them again.")
    return setup({"type": "b2", "account": key_id, "key": key, "hard_delete": "false"}, bucket, path, rclone, folder)
