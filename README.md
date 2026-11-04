# jobkit

Scheduled-job history kept in an app's own SQLite (`jobkit_jobs`, `jobkit_runs`, beside the app's own tables), with a
report in one shared shape: what each job does, its schedule, the last run, the last good run, and failures in a row.

Each app keeps its own database and its own jobs page (installs stay independent): it serves `Jobs.report()` at
`GET /api/jobs` and `page_html(app_name)` at `GET /jobs`.

```python
from jobkit import Jobs

jobs = Jobs(data_dir / "app.db")
jobs.define("new-episodes", "New episodes of added shows into Jellyfin", "hourly")
with jobs.run("new-episodes") as run:  # failed (with the error) if the block raises
    run.summary = "2 new episodes"
```

Each app also serves the jobs page at `/jobs` (`page_html(app_name)`, one self-contained file that reads its `api/jobs`).

Shell jobs (launchd), with the output passed through unchanged:

```
job-run --db "$DB" --describe "Nightly backup to Backblaze" --schedule "daily 3:00" backup -- bash backup.sh
```

A run whose process is gone (the Mac restarted mid-run) shows as `interrupted`. Runs are kept 90 days.

Off-site backups: each app backs up its own data with `job-backup` (rclone sync, then a check that the copy
matches), run under `job-run` so the run lands on its jobs page. For Backblaze, point it at a crypt remote over a B2
bucket, so files and names are encrypted before they leave the machine:

```
job-run --db "$DB" --app photo-app --describe "Photos to Backblaze (encrypted)" --schedule "daily 3:00" \
    photos-offsite -- job-backup --config ~/.config/rclone/backup.conf --to backup:photo-app "/Volumes/Drive/Photos"
```

A missing or empty folder (drive unplugged) is never synced over the copy, and a run that finds the last one still
going leaves it to finish. `--cryptcheck` compares checksums instead of sizes (reads every file; slow).

Setting up the remote from an app's Backup page: `jobkit.offsite` keeps one encrypted remote per Mac (`backup`, in
the owner-only `~/.config/rclone/backup.conf`), made once and reused by every app (each into `backup:<app>`).
`offsite.status()` says whether it exists and where it writes; `offsite.setup_b2(key_id, key, bucket)` checks the
Backblaze key against the bucket (passed to rclone in environment variables, never on a command line), writes the
config and returns the recovery code: the encryption password, made there, which the app shows once. Without it
nobody can restore. Another storage rclone knows works through `offsite.setup({"type": ..., ...}, bucket)`.

An app with no web server of its own (only shell jobs) gets the same page from `job-page`, standard library only:

```
job-page --db "$DB" --app "My App" --port 8089 --allow-host app.example.com
```

It serves /jobs, /api/jobs, Run now and Pause on 127.0.0.1, and answers only to loopback names and each
`--allow-host` (the name your proxy serves it under). Any other Host gets 421, so a web page that points its own name
at this machine (DNS rebinding) cannot read the runs or press the buttons. An app serving its own routes should
check the Host the same way. To serve it under a domain of your own: [Your own domain with Cloudflare](https://gist.github.com/shrshk/58b710264907d1e229675a2313f24b0e).

From the jobs page, **Pause** stops a job's schedule (`job-run` skips a paused job; an in-app schedule checks
`jobs.paused(name)`), and **Run now** starts it: an in-app job's `jobs.register(name, fn)`, or a launchd job's
`launchctl kickstart`. The app serves `POST api/jobs/{name}/run` and `POST api/jobs/{name}/pause` (`{"paused": true}`)
with `run_now()` and `pause()`; `report()` tells the page which buttons a job has (`actions`). Both POSTs must refuse any body
that is not `Content-Type: application/json` (415): a JSON POST from another site needs a CORS preflight, so this
stops another site's page from running or pausing jobs. `job-page` does this; an app serving its own routes must too.

When a job fails twice in a row, `Jobs(db, app, notify=...)` sends one notice (not one per failure). Where it
goes is `JOBKIT_NOTIFY_URL` or the line in `~/.config/jobkit/notify-url`: `openclaw:<node>` pushes the title to that
phone through the OpenClaw app (`openclaw nodes push`, no model turn; the body only says "Details on the jobs page",
since the push travels through OpenClaw's relay); any other URL gets the text POSTed (ntfy-style, `Title` header).
Nothing is sent if neither is set.

## Prerequisites

`jobkit.prereqs` checks what an app needs on the Mac and installs what is missing when the person asks: each part
(`Part`: key, name, why, a check returning `(ok, detail)`, an install command as an argument list, a link, an
"after" line for what only the person can do) and an `Installer` that runs one install in a thread and reports its
progress. Common parts are built in (`brew()`, `uv()`, `rclone()`, `tailscale()`); an app adds its own.

```python
from jobkit import prereqs

setup = prereqs.Installer([prereqs.brew(), prereqs.uv(), prereqs.tailscale(optional=True)])
setup.report()      # each part: ok, what was found, its install command, link, "after", install progress
setup.start("uv")   # `brew install uv` in a thread; ValueError says why not (no Homebrew, already there)
```

Serve the Install button only to `prereqs.local(client_host, forwarded)`: requests straight from the Mac, not a phone
on the network. `prereqs.tailscale_name()` is the Mac's Tailscale name (`mac.tail1234.ts.net`), for allowing it in
a Host check without a domain.

