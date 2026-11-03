"""Scheduled-job history in an app's own SQLite (tables jobkit_jobs and jobkit_runs, so they sit beside the app's
own tables): which jobs exist, every run, and a report in one shared shape.

Each app keeps these tables in its own database (installs stay independent); every app serves report() at
GET /api/jobs, and the jobs page collects them over HTTP.

    jobs = Jobs(data / "app.db")
    jobs.define("new-episodes", "New episodes of added shows into Jellyfin", "hourly")
    with jobs.run("new-episodes") as run:
        run.summary = f"{n} new episodes"

Shell jobs use the job-run command (jobkit.cli).

From the jobs page: Pause (a paused job's schedule skips it: job-run exits at once, in-app schedules ask paused()),
and Run now (an in-app job's registered function, or a launchd job's kickstart). When a job fails twice in a row,
notify() is called once. By default it goes where JOBKIT_NOTIFY_URL or ~/.config/jobkit/notify-url says, if set:
"openclaw:<node>" is a push to that phone through the OpenClaw app (title only: the push travels through OpenClaw's
relay, so details stay on the jobs page); any other URL gets a POST of the text (ntfy-style).
"""

import html
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobkit_jobs (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    schedule TEXT NOT NULL DEFAULT '',
    launchd_label TEXT NOT NULL DEFAULT '',
    log TEXT NOT NULL DEFAULT '',
    paused INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobkit_runs (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL REFERENCES jobkit_jobs (name),
    started TEXT NOT NULL,
    finished TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed', 'interrupted')),
    exit_code INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    pid INTEGER
);
CREATE INDEX IF NOT EXISTS jobkit_runs_job_started ON jobkit_runs (job, started);
"""
KEEP_DAYS = 90
NOTIFY_FILE = Path.home() / ".config/jobkit/notify-url"


def notify_url() -> str:
    url = os.environ.get("JOBKIT_NOTIFY_URL", "")
    if not url and NOTIFY_FILE.exists():
        url = NOTIFY_FILE.read_text().strip()
    return url


def post_notice(title: str, text: str) -> None:
    """The default notice, to an OpenClaw phone or a notify URL. Never raises: a job's result is already recorded and
    a notice that cannot be sent must not change it."""
    url = notify_url()
    if url.startswith("openclaw:"):
        push_openclaw(url.removeprefix("openclaw:") or "iPhone", title)
    elif url:
        req = urllib.request.Request(url, data=text.encode(), headers={"Title": title}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except OSError as e:
            print(f"jobkit: notice not sent ({e})")


def push_openclaw(node: str, title: str) -> None:
    """A push to a phone paired with the local OpenClaw gateway; no model turn. launchd jobs lack Homebrew's PATH,
    which the openclaw command (a node script) needs."""
    path = os.pathsep.join([os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"])
    cmd = shutil.which("openclaw", path=path)
    if not cmd:
        print("jobkit: notice not sent (openclaw not found)")
        return
    args = [cmd, "nodes", "push", "--node", node, "--title", title, "--body", "Details on the jobs page."]
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=60, env=os.environ | {"PATH": path})
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"jobkit: notice not sent ({e})")
        return
    if done.returncode:
        print(f"jobkit: notice not sent ({(done.stderr or done.stdout).strip()[-200:]})")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # someone else's process: it exists
        return True
    return True


class Run:
    """What a job says about itself while it runs: a one-line summary ("1,204 files, 3.1 GB")."""

    def __init__(self, run_id: int):
        self.id = run_id
        self.summary = ""


class Jobs:
    def __init__(self, path: Path | str, app: str = "", notify: Callable[[str, str], None] = post_notice):
        self.app = app
        self.notify = notify
        self.actions: dict[str, Callable[[], None]] = {}
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.execute("PRAGMA journal_mode=WAL")  # jobs and the app write at the same time
            self.db.executescript(SCHEMA)

    def define(self, name: str, description: str, schedule: str, launchd_label: str = "", log: str = "") -> None:
        """Adds the job or updates what it says about itself; paused stays as set."""
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO jobkit_jobs (name, description, schedule, launchd_label, log) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET description = excluded.description, "
                "schedule = excluded.schedule, launchd_label = excluded.launchd_label, log = excluded.log",
                (name, description, schedule, launchd_label, log),
            )

    def start(self, job: str, pid: int | None = None) -> int:
        """A new run. An older run of this job still marked running whose process is gone was cut off."""
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO jobkit_jobs (name) VALUES (?)", (job,))
            for old in self.db.execute("SELECT id, pid FROM jobkit_runs WHERE job = ? AND status = 'running'", (job,)):
                if not alive(old["pid"]):
                    self.db.execute("UPDATE jobkit_runs SET status = 'interrupted' WHERE id = ?", (old["id"],))
            cur = self.db.execute(
                "INSERT INTO jobkit_runs (job, started, status, host, pid) VALUES (?, ?, 'running', ?, ?)",
                (job, now(), socket.gethostname(), pid or os.getpid()),
            )
            self.db.execute(
                "DELETE FROM jobkit_runs WHERE started < ? AND status != 'running'",
                ((datetime.now(UTC) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds"),),
            )
            return cur.lastrowid

    def finish(self, run_id: int, exit_code: int, summary: str = "", error: str = "") -> None:
        with self.lock, self.db:
            self.db.execute(
                "UPDATE jobkit_runs SET finished = ?, status = ?, exit_code = ?, summary = ?, error = ? WHERE id = ?",
                (now(), "ok" if exit_code == 0 else "failed", exit_code, summary[:200], error[:300], run_id),
            )
            job = self.db.execute("SELECT job FROM jobkit_runs WHERE id = ?", (run_id,)).fetchone()["job"]
            recent = self.db.execute(
                "SELECT status FROM jobkit_runs WHERE job = ? AND status != 'running' ORDER BY id DESC LIMIT 3", (job,)
            ).fetchall()
        # A notice at exactly the second failure in a row: one per bad streak, not one an hour.
        statuses = [r["status"] for r in recent]
        streak = next((n for n, st in enumerate(statuses) if st == "ok"), len(statuses))
        if exit_code and streak == 2:
            where = f"{self.app}: " if self.app else ""
            # The title only: an error's text can hold a link with a token, and a notice service may be public.
            self.notify(f"{where}{job} failed twice in a row", "Details on the jobs page.")

    def pause(self, job: str, paused: bool = True) -> None:
        with self.lock, self.db:
            if not self.db.execute("UPDATE jobkit_jobs SET paused = ? WHERE name = ?", (int(paused), job)).rowcount:
                raise LookupError(job)

    def paused(self, job: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT paused FROM jobkit_jobs WHERE name = ?", (job,)).fetchone()
        return bool(row and row["paused"])

    def register(self, job: str, action: Callable[[], None]) -> None:
        """What Run now does for an in-app job (run in a thread; the action records its own run)."""
        self.actions[job] = action

    def run_now(self, job: str) -> None:
        """Runs the job now: its registered action, or its launchd job (whose job-run skips it while paused, so the
        page offers Run now only when the job is not paused). LookupError if it has neither."""
        with self.lock:
            row = self.db.execute("SELECT launchd_label FROM jobkit_jobs WHERE name = ?", (job,)).fetchone()
        if job in self.actions:
            threading.Thread(target=self.actions[job], daemon=True).start()
        elif row and row["launchd_label"]:
            subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{row['launchd_label']}"], check=True)
        else:
            raise LookupError(job)

    @contextmanager
    def run(self, job: str):
        """A run of an in-app job: ok when the block ends, failed (with the error) when it raises."""
        r = Run(self.start(job))
        try:
            yield r
        except BaseException as e:
            self.finish(r.id, 1, r.summary, f"{type(e).__name__}: {e}")
            raise
        self.finish(r.id, 0, r.summary)

    def report(self) -> list[dict]:
        """Every job with its last run, its last good run and how many runs in a row failed: GET /api/jobs."""
        with self.lock:
            jobs = [dict(j) for j in self.db.execute("SELECT * FROM jobkit_jobs ORDER BY name").fetchall()]
            runs = {
                j["name"]: [
                    dict(r)
                    for r in self.db.execute(
                        "SELECT * FROM jobkit_runs WHERE job = ? ORDER BY id DESC LIMIT 50", (j["name"],)
                    )
                ]
                for j in jobs
            }
        return [
            summarize(j, runs[j["name"]])
            | {"actions": {"pause": True, "run": j["name"] in self.actions or bool(j["launchd_label"])}}
            for j in jobs
        ]


def summarize(job: dict, runs: list[dict]) -> dict:
    """One job in the shared report shape, from its runs newest first. Apps that keep their own run history (with
    status running / ok / failed) use this too, so every app's /api/jobs reads the same."""
    last = dict(runs[0]) if runs else None
    if last and last["status"] == "running" and "pid" in last and not alive(last["pid"]):
        last["status"] = "interrupted"
    failing = 0
    for r in runs:
        if r["status"] == "running":
            continue
        if r["status"] == "ok":
            break
        failing += 1
    return {
        "name": job["name"],
        "description": job.get("description", ""),
        "schedule": job.get("schedule", ""),
        "launchd_label": job.get("launchd_label", ""),
        "log": job.get("log", ""),
        "paused": bool(job.get("paused")),
        "running": bool(last and last["status"] == "running"),
        "last": {k: last.get(k) or "" for k in ("started", "finished", "status", "summary", "error")} if last else None,
        "last_ok": next((r["finished"] for r in runs if r["status"] == "ok"), None),
        "failures_in_a_row": failing,
    }


def page_html(app: str) -> str:
    """The jobs page each app serves at /jobs (next to its /api/jobs, which the page reads): one self-contained file,
    light and dark, refreshing every 30 seconds."""
    page = resources.files("jobkit").joinpath("page.html").read_text()
    return page.replace("{{TITLE}}", html.escape(app))
