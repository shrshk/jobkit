import fcntl
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from jobkit import Jobs, page_html
from jobkit import backup as backup_mod
from jobkit.cli import main


def test_an_in_app_job_records_ok_and_failed_runs(tmp_path):
    jobs = Jobs(tmp_path / "app.db")
    jobs.define("new-episodes", "New episodes into Jellyfin", "hourly")
    with jobs.run("new-episodes") as run:
        run.summary = "2 new episodes"
    with pytest.raises(OSError), jobs.run("new-episodes"):
        raise OSError("drive gone")
    [job] = jobs.report()
    assert job["schedule"] == "hourly" and job["last"]["status"] == "failed"
    assert job["last"]["error"] == "OSError: drive gone" and job["failures_in_a_row"] == 1
    assert job["last_ok"] is not None and not job["running"]


def test_failures_in_a_row_count_until_the_last_good_run(tmp_path):
    jobs = Jobs(tmp_path / "app.db")
    for code in (1, 0, 1, 1):
        jobs.finish(jobs.start("backup"), code)
    assert jobs.report()[0]["failures_in_a_row"] == 2


def test_a_run_whose_process_died_shows_as_interrupted(tmp_path):
    jobs = Jobs(tmp_path / "app.db")
    jobs.start("backup", pid=999_999_9)  # no such process: the Mac restarted mid-run
    [job] = jobs.report()
    assert job["last"]["status"] == "interrupted" and not job["running"]
    jobs.finish(jobs.start("backup"), 0)  # the next run closes it for good
    statuses = [
        r[0] for r in sqlite3.connect(tmp_path / "app.db").execute("SELECT status FROM jobkit_runs ORDER BY id")
    ]
    assert statuses == ["interrupted", "ok"]


def test_job_run_passes_output_through_and_records_the_shell_job(tmp_path, capsys):
    db = str(tmp_path / "app.db")
    script = "import sys; print('synced 3 files'); print('rclone: quota exceeded', file=sys.stderr); sys.exit(3)"
    code = main(
        [
            "--db",
            db,
            "--describe",
            "Nightly backup",
            "--schedule",
            "daily 3:00",
            "backup",
            "--",
            sys.executable,
            "-c",
            script,
        ]
    )
    out = capsys.readouterr()
    assert code == 3 and "synced 3 files" in out.out and "quota exceeded" in out.err
    [job] = Jobs(db).report()
    assert job["description"] == "Nightly backup" and job["schedule"] == "daily 3:00"
    assert job["last"]["status"] == "failed" and job["last"]["summary"] == "synced 3 files"
    assert job["last"]["error"] == "rclone: quota exceeded"


def test_job_run_records_a_command_that_cannot_start(tmp_path):
    db = str(tmp_path / "app.db")
    assert main(["--db", db, "backup", "--", str(tmp_path / "missing")]) == 127
    assert Jobs(db).report()[0]["last"]["status"] == "failed"


def test_the_tables_sit_beside_an_apps_own_runs_table(tmp_path):
    app_db = sqlite3.connect(tmp_path / "app.db")
    app_db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, rule_id INTEGER, status TEXT)")  # an app's own
    app_db.commit()
    jobs = Jobs(tmp_path / "app.db")
    jobs.finish(jobs.start("backup"), 0, "3 files")
    assert jobs.report()[0]["last"]["summary"] == "3 files"


def test_the_jobs_page_names_the_app_safely_and_reads_its_own_report():
    page = page_html('My <App> & "co"')
    assert "My &lt;App&gt; &amp; &quot;co&quot;" in page and "<App>" not in page
    assert 'fetch("api/jobs"' in page and "{{TITLE}}" not in page


def test_a_notice_goes_out_once_when_a_job_fails_twice_in_a_row(tmp_path):
    sent = []
    jobs = Jobs(tmp_path / "app.db", "media-app", notify=lambda title, text: sent.append((title, text)))
    for code, error in ((1, "drive gone"), (1, "drive still gone"), (1, "and again"), (0, ""), (1, "x"), (1, "y")):
        jobs.finish(jobs.start("backup"), code, error=error)
    assert sent == [
        ("media-app: backup failed twice in a row", "Details on the jobs page."),
        ("media-app: backup failed twice in a row", "Details on the jobs page."),
    ]  # not again at the third failure; a good run starts a new streak


def test_the_default_notice_posts_to_the_notify_url_and_never_raises(monkeypatch, tmp_path):
    import urllib.request

    import jobkit

    posted = []

    class Answer:
        def close(self):
            pass

    def urlopen(req, timeout):
        if "down" in req.full_url:
            raise OSError("no route")
        posted.append((req.full_url, req.get_header("Title"), req.data))
        return Answer()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(jobkit, "NOTIFY_FILE", tmp_path / "none")
    monkeypatch.delenv("JOBKIT_NOTIFY_URL", raising=False)
    jobkit.post_notice("t", "body")  # no URL set: nothing to do
    monkeypatch.setenv("JOBKIT_NOTIFY_URL", "http://ntfy.local/jobs")
    jobkit.post_notice("backup failed twice in a row", "drive gone")
    monkeypatch.setenv("JOBKIT_NOTIFY_URL", "http://down.local/jobs")
    jobkit.post_notice("t", "body")
    assert posted == [("http://ntfy.local/jobs", "backup failed twice in a row", b"drive gone")]


def test_an_openclaw_notice_pushes_the_title_only(monkeypatch, tmp_path):
    import subprocess

    import jobkit

    calls = []

    def run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0 if len(calls) == 1 else 1, "", "no APNs registration")

    monkeypatch.setattr(jobkit.subprocess, "run", run)
    monkeypatch.setattr(jobkit.shutil, "which", lambda name, path: "/bin/openclaw")
    monkeypatch.setenv("JOBKIT_NOTIFY_URL", "openclaw:iPhone")
    jobkit.post_notice("photos: backup failed twice in a row", "/Volumes/Drive/Personal: no such file")
    jobkit.post_notice("again", "x")  # a failed push is reported, never raised
    assert calls[0][1:7] == ["nodes", "push", "--node", "iPhone", "--title", "photos: backup failed twice in a row"]
    assert not any("Personal" in a for a in calls[0])  # details never ride the push relay


def test_a_paused_shell_job_is_skipped_and_resumes(tmp_path, capsys):
    db = str(tmp_path / "app.db")
    jobs = Jobs(db)
    jobs.define("backup", "Nightly backup", "daily 3:00")
    jobs.pause("backup")
    marker = tmp_path / "ran"
    cmd = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"]
    assert main(["--db", db, "backup", "--", *cmd]) == 0
    assert not marker.exists() and jobs.report()[0]["last"] is None and jobs.report()[0]["paused"]
    jobs.pause("backup", False)
    assert main(["--db", db, "backup", "--", *cmd]) == 0
    assert marker.exists() and jobs.report()[0]["last"]["status"] == "ok"
    with pytest.raises(LookupError):
        jobs.pause("no-such-job")


def test_run_now_starts_the_registered_action_or_the_launchd_job(tmp_path, monkeypatch):
    import subprocess
    import threading

    jobs = Jobs(tmp_path / "app.db")
    jobs.define("new-episodes", "New episodes", "hourly")
    jobs.define("backup", "Nightly backup", "daily 3:00", launchd_label="dev.app.backup")
    jobs.define("manual", "Nothing to start", "")
    done = threading.Event()
    jobs.register("new-episodes", done.set)
    kicked = []
    monkeypatch.setattr(subprocess, "run", lambda args, check: kicked.append(args))
    jobs.run_now("new-episodes")
    assert done.wait(2)
    jobs.run_now("backup")
    assert kicked == [["launchctl", "kickstart", f"gui/{os.getuid()}/dev.app.backup"]]
    with pytest.raises(LookupError):
        jobs.run_now("manual")
    actions = {j["name"]: j["actions"] for j in jobs.report()}
    assert actions == {
        "backup": {"pause": True, "run": True},
        "manual": {"pause": True, "run": False},
        "new-episodes": {"pause": True, "run": True},
    }


def test_job_page_serves_the_page_the_report_and_its_buttons(tmp_path, monkeypatch):
    import json
    import subprocess
    import threading
    import urllib.request
    from urllib.error import HTTPError

    from jobkit.page import serve

    db = str(tmp_path / "jobs.db")
    Jobs(db).define("backup", "Encrypted backup", "daily 3:00", launchd_label="dev.app.backup")
    assert main(["--db", db, "backup", "--", sys.executable, "-c", "print('done')"]) == 0
    server = serve(db, "My App", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path, body=None, kind="application/json"):
        req = urllib.request.Request(
            base + path, data=json.dumps(body or {}).encode(), headers={"Content-Type": kind}, method="POST"
        )
        return urllib.request.urlopen(req).status

    try:
        assert "My App" in urllib.request.urlopen(base + "/jobs").read().decode()
        report = json.load(urllib.request.urlopen(base + "/api/jobs"))
        assert report["app"] == "My App"
        assert [(j["name"], j["last"]["summary"]) for j in report["jobs"]] == [("backup", "done")]
        assert post("/api/jobs/backup/pause", {"paused": True}) == 204
        assert json.load(urllib.request.urlopen(base + "/api/jobs"))["jobs"][0]["paused"] is True
        kicked = []
        monkeypatch.setattr(subprocess, "run", lambda args, check: kicked.append(args))
        assert post("/api/jobs/backup/run") == 202
        assert kicked == [["launchctl", "kickstart", f"gui/{os.getuid()}/dev.app.backup"]]
        with pytest.raises(HTTPError) as e:
            post("/api/jobs/no%20such/run")
        assert e.value.code == 404
        # Another site's page can POST text/plain without asking (no CORS preflight): such a POST must not run or
        # pause anything.
        kicked.clear()
        for path in ("/api/jobs/backup/run", "/api/jobs/backup/pause"):
            with pytest.raises(HTTPError) as e:
                post(path, {"paused": False}, kind="text/plain")
            assert e.value.code == 415
        assert kicked == [] and json.load(urllib.request.urlopen(base + "/api/jobs"))["jobs"][0]["paused"] is True
    finally:
        server.shutdown()


def test_job_page_answers_only_to_this_machine_and_the_proxys_names(tmp_path):
    import http.client
    import threading

    from jobkit.page import serve

    db = str(tmp_path / "jobs.db")
    Jobs(db).define("backup", "Encrypted backup", "daily 3:00", launchd_label="dev.app.backup")
    server = serve(db, "My App", port=0, allow_hosts=("app.example.com",))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def status(method, host):
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
        c.request(
            method,
            "/api/jobs/backup/pause" if method == "POST" else "/api/jobs",
            body=b'{"paused": true}',
            headers={"Host": host, "Content-Type": "application/json"},
        )
        return c.getresponse().status

    try:
        for host in ("127.0.0.1:8089", "localhost", "[::1]:8089", "App.Example.com"):
            assert status("GET", host) == 200, host
        # A page that pointed its own name at this machine (DNS rebinding) reads nothing and presses nothing.
        assert status("GET", "evil.example.net") == 421 and status("POST", "evil.example.net") == 421
        assert Jobs(db).paused("backup") is False
    finally:
        server.shutdown()


def test_job_page_is_never_framed_and_takes_only_a_small_plain_body(tmp_path):
    import http.client
    import threading

    from jobkit.page import serve

    db = str(tmp_path / "jobs.db")
    Jobs(db).define("backup", "Encrypted backup", "daily 3:00")
    server = serve(db, "My App", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(method, path, body=b"", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        c.request(method, path, body=body, headers={"Content-Type": "application/json", **(headers or {})})
        r = c.getresponse()
        return r.status, r.headers

    try:
        status, headers = call("GET", "/jobs")
        assert (
            status == 200
            and headers["X-Frame-Options"] == "DENY"
            and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        )
        assert call("POST", "/api/jobs/backup/pause", b'{"paused": "false"}')[0] == 400  # a string is not a choice
        assert call("POST", "/api/jobs/backup/pause", b"[]")[0] == 400
        assert call("POST", "/api/jobs/backup/pause", b"{}", {"Content-Length": "999999999"})[0] == 400
        assert Jobs(db).paused("backup") is False
        assert call("POST", "/api/jobs/backup/pause", b'{"paused": true}')[0] == 204
    finally:
        server.shutdown()


rclone_missing = pytest.mark.skipif(not shutil.which("rclone"), reason="needs rclone")


def make_folder(path, files):
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    return path


@rclone_missing
def test_job_backup_copies_each_folder_checks_it_and_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(backup_mod, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "none.conf"))
    a = make_folder(tmp_path / "library", {"a.jpg": "aaaa", "sub/b.mov": "bb", ".DS_Store": "x"})
    b = make_folder(tmp_path / "dumps", {"db.sql.gz": "dump"})
    remote = tmp_path / "remote"
    assert backup_mod.main([str(a), str(b), "--to", str(remote)]) == 0
    assert (remote / "library/sub/b.mov").read_text() == "bb" and (remote / "dumps/db.sql.gz").exists()
    assert not (remote / "library/.DS_Store").exists()
    assert capsys.readouterr().out.strip().splitlines()[-1] == "2 folders, 3 files, 10 B, copy matches"


@rclone_missing
def test_job_backup_never_syncs_a_missing_folder_over_the_copy(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(backup_mod, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "none.conf"))
    remote = make_folder(tmp_path / "remote", {"a.jpg": "aaaa"})
    (tmp_path / "unplugged").mkdir()  # the drive's mount point, empty
    assert backup_mod.main([str(tmp_path / "unplugged"), "--to", str(remote)]) == 1
    assert (remote / "a.jpg").read_text() == "aaaa"
    assert capsys.readouterr().out.strip().splitlines()[-1].endswith("unplugged missing")


@rclone_missing
def test_job_backup_never_syncs_a_folder_that_holds_only_skipped_files(tmp_path, monkeypatch, capsys):
    # An unmounted drive's mount point that Finder wrote into: something there, but nothing rclone would send.
    monkeypatch.setattr(backup_mod, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "none.conf"))
    remote = make_folder(tmp_path / "remote", {"a.jpg": "aaaa"})
    mount = make_folder(tmp_path / "Photos", {".DS_Store": "x", "._a.jpg": "x"})
    assert backup_mod.main([str(mount), "--to", str(remote)]) == 1
    assert (remote / "a.jpg").read_text() == "aaaa"


@rclone_missing
def test_job_backup_refuses_two_folders_with_one_name(tmp_path, monkeypatch):
    # Both would go to to/Photos, each deleting the other's copy on every run.
    monkeypatch.setattr(backup_mod, "LOCK_DIR", tmp_path / "locks")
    one = make_folder(tmp_path / "a" / "Photos", {"1.jpg": "1"})
    two = make_folder(tmp_path / "b" / "Photos", {"2.jpg": "2"})
    code, summary = backup_mod.backup([str(one), str(two)], str(tmp_path / "remote"))
    assert code == 1 and "share a name" in summary and not (tmp_path / "remote").exists()


def test_job_backup_leaves_a_running_backup_to_finish(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(backup_mod, "LOCK_DIR", tmp_path)
    lock = tmp_path / f"backup-{hashlib.sha256(b'backup:photo-app').hexdigest()[:12]}.lock"
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        code, summary = backup_mod.backup([str(tmp_path)], "backup:photo-app", rclone_cmd="false")
    assert (code, summary) == (0, "skipped: the last backup is still running")


@rclone_missing
def test_offsite_setup_encrypts_and_the_recovery_code_alone_restores(tmp_path, monkeypatch):
    from jobkit import offsite

    monkeypatch.setattr(backup_mod, "LOCK_DIR", tmp_path / "locks")
    conf = tmp_path / "rclone" / "backup.conf"
    bucket = tmp_path / "bucket"  # a local folder stands in for the B2 bucket
    bucket.mkdir()
    assert offsite.status(conf) == {"configured": False, "where": ""}
    code = offsite.setup({"type": "local"}, str(bucket), conf)
    assert oct(conf.stat().st_mode)[-3:] == "600" and code not in conf.read_text()
    assert offsite.status(conf) == {"configured": True, "where": f"store:{bucket}/backups"}
    with pytest.raises(offsite.SetupError, match="already"):  # the code restores what is uploaded: keep it
        offsite.setup({"type": "local"}, str(bucket), conf)

    library = make_folder(tmp_path / "library", {"IMG_0001.jpg": "photo"})
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))
    assert backup_mod.backup([str(library)], "backup:photos")[0] == 0
    stored = [p.name for p in bucket.rglob("*") if p.is_file()]
    assert len(stored) == 1 and "IMG_0001" not in stored[0]  # names are encrypted too
    assert "photo" not in next(bucket.rglob(stored[0])).read_text(errors="ignore")

    # A new Mac with only the recovery code (and access to the bucket) gets the files back.
    fresh = tmp_path / "fresh.conf"
    obscured = subprocess.run(["rclone", "obscure", "-"], input=code, capture_output=True, text=True, check=True)
    fresh.write_text(f"[r]\ntype = crypt\nremote = {bucket}/backups\npassword = {obscured.stdout.strip()}\n")
    out = tmp_path / "restored"
    subprocess.run(["rclone", "copy", "r:photos", str(out), "--config", str(fresh)], check=True, capture_output=True)
    assert (out / "IMG_0001.jpg").read_text() == "photo"


@rclone_missing
def test_offsite_setup_says_what_is_wrong_and_writes_nothing(tmp_path):
    from jobkit import offsite

    conf = tmp_path / "backup.conf"
    with pytest.raises(offsite.SetupError, match="No bucket named"):
        offsite.setup({"type": "local"}, str(tmp_path / "no-such-bucket"), conf)
    with pytest.raises(offsite.SetupError, match="all needed"):
        offsite.setup_b2("id", " ", "bucket", conf)
    assert not conf.exists()


@rclone_missing
def test_offsite_setup_takes_nothing_that_could_rewrite_the_config_or_leak_the_key(tmp_path):
    from jobkit import offsite

    conf = tmp_path / "backup.conf"
    bucket = tmp_path / "bucket"
    bucket.mkdir()
    # A line break would start a section of its own in rclone's reading (an extra remote, or a new [backup]).
    for bad in (
        {"key_id": "id\n[evil]\ntype = local", "key": "k", "bucket": "b"},
        {"key_id": "id", "key": "k", "bucket": "b/../other"},
        {"key_id": "id", "key": "k\x00", "bucket": "b"},
    ):
        with pytest.raises(offsite.SetupError):
            offsite.setup_b2(path=conf, **bad)
    with pytest.raises(offsite.SetupError, match="line break"):
        offsite.setup({"type": "local", "x": "1\n[backup]\ntype = local"}, str(bucket), conf)
    assert not conf.exists()
    # A % is just a character (configparser would otherwise raise, with the key in its message).
    offsite.setup({"type": "local", "note": "50%-off"}, str(bucket), conf)
    assert offsite.status(conf)["configured"] and "50%-off" in conf.read_text()


def test_offsite_setup_never_replaces_a_config_it_cannot_read(tmp_path):
    from jobkit import offsite

    conf = tmp_path / "backup.conf"
    conf.write_text("RCLONE_ENCRYPT_V0:\nsealed-config-holding-the-old-password\n")
    with pytest.raises(offsite.SetupError, match="move it away"):
        offsite.setup({"type": "local"}, str(tmp_path), conf, rclone="false")
    assert "old-password" in conf.read_text()


def fake_command(folder, name, script):
    path = folder / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)
    return path


def test_prereqs_install_a_missing_part_with_homebrew_and_see_it_found(tmp_path, monkeypatch):
    import time

    from jobkit import prereqs

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(prereqs, "BREW_DIRS", [])
    setup = prereqs.Installer([prereqs.brew(), prereqs.uv()])
    with pytest.raises(ValueError, match="Homebrew first"):
        setup.start("uv")
    with pytest.raises(LookupError):
        setup.start("brew")  # Homebrew's installer is the person's to run (a link), never started from here

    # A stand-in Homebrew whose `brew install uv` puts a uv command on PATH.
    fake_command(
        bin_dir,
        "brew",
        f'[ "$1 $2" = "install uv" ] && printf "#!/bin/sh\\n" > {bin_dir}/uv && chmod +x {bin_dir}/uv && echo installed uv\n',
    )
    assert {p["key"]: p["ok"] for p in setup.report()} == {"brew": True, "uv": False}
    setup.start("uv")
    for _ in range(50):
        if not setup.report()[1]["installing"].get("running"):
            break
        time.sleep(0.1)
    uv_row = setup.report()[1]
    assert uv_row["ok"] is True and uv_row["installing"]["ok"] is True and "installed uv" in uv_row["installing"]["log"]
    with pytest.raises(ValueError, match="already there"):
        setup.start("uv")


def test_prereqs_tailscale_name_and_state_and_who_may_install(tmp_path, monkeypatch):
    from jobkit import prereqs

    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setattr(prereqs, "BREW_DIRS", [])
    monkeypatch.setattr(prereqs, "TAILSCALE_APP", str(tmp_path / "no-app"))
    assert prereqs.tailscale_name() == "" and prereqs.tailscale().check() == (False, "not installed")
    fake_command(
        tmp_path,
        "tailscale",
        """echo '{"BackendState": "NeedsLogin", "Self": {"DNSName": "Mac.tail1234.ts.net."}}'\n""",
    )
    assert prereqs.tailscale_name() == "mac.tail1234.ts.net"
    assert prereqs.tailscale().check() == (False, "installed, not connected (NeedsLogin)")
    assert prereqs.local("127.0.0.1", forwarded=False) and prereqs.local("::1", forwarded=False)
    assert not prereqs.local("127.0.0.1", forwarded=True) and not prereqs.local("100.64.0.7", forwarded=False)


FAKE_TAILSCALE = """#!/bin/sh
# tailscale, as far as serving goes: the served ports live in a file next to it
STATE="$(dirname "$0")/served.json"
[ -f "$STATE" ] || echo '{}' > "$STATE"
case "$1 $2" in
  "status --json") echo '{"BackendState": "Running", "Self": {"DNSName": "mac.tail1234.ts.net."}}' ;;
  "serve status") cat "$STATE" ;;
  "serve --bg") python3 - "$STATE" "$3" "$4" <<'PY'
import json, sys
state, port, target = sys.argv[1], sys.argv[2].split("=")[1], sys.argv[3]
d = json.load(open(state)); d.setdefault("Web", {})[f"mac.tail1234.ts.net:{port}"] = {"Handlers": {"/": {"Proxy": target}}}
json.dump(d, open(state, "w"))
PY
  ;;
  serve\ --https=*) python3 - "$STATE" "$2" <<'PY'
import json, sys
state, port = sys.argv[1], sys.argv[2].split("=")[1]
d = json.load(open(state)); d.get("Web", {}).pop(f"mac.tail1234.ts.net:{port}", None); json.dump(d, open(state, "w"))
PY
  ;;
esac
"""


def test_tailscale_serve_puts_an_app_on_the_tailnet_without_taking_another_apps_port(tmp_path, monkeypatch):
    from jobkit import prereqs

    tool = tmp_path / "tailscale"
    tool.write_text(FAKE_TAILSCALE)
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setattr(prereqs, "BREW_DIRS", [])
    assert prereqs.served() == {}
    assert prereqs.serve(8726, "http://127.0.0.1:8723") == "https://mac.tail1234.ts.net:8726"
    assert prereqs.serve(8726, "http://127.0.0.1:8723")  # again: the same, fine
    assert prereqs.served() == {8726: "http://127.0.0.1:8723"}
    with pytest.raises(ValueError, match="already goes to"):  # another app's port is never taken over
        prereqs.serve(8726, "http://127.0.0.1:2283")
    prereqs.unserve(8726)
    assert prereqs.served() == {}
