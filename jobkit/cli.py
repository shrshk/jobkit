"""job-run: runs a shell job and records the run in an app's database.

    job-run --db ~/Library/Application\\ Support/app/app.db --schedule "daily 3:00" \\
        --describe "Nightly backup to Backblaze" backup -- bash backup.sh

The job's output passes through unchanged (so its log stays as it was). Its last output line becomes the summary
and its last error line the error; the exit code is the job's own. A job paused on the jobs page is skipped (exit 0,
nothing recorded). --app names the app in the notice sent when the job fails twice in a row.
"""

import argparse
import subprocess
import sys
import threading

from jobkit import Jobs


def tee(stream, out, last: list[str]) -> None:
    for line in iter(stream.readline, ""):
        out.write(line)
        out.flush()
        if line.strip():
            last[0] = line.strip()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--" not in argv:
        print(
            "usage: job-run --db PATH [--app NAME] [--describe TEXT] [--schedule TEXT] NAME -- COMMAND...",
            file=sys.stderr,
        )
        return 2
    split = argv.index("--")
    p = argparse.ArgumentParser(prog="job-run")
    p.add_argument("--db", required=True)
    p.add_argument("--describe", default="")
    p.add_argument("--schedule", default="")
    p.add_argument("--launchd-label", default="")
    p.add_argument("--log", default="")
    p.add_argument("--app", default="")
    p.add_argument("name")
    args, command = p.parse_args(argv[:split]), argv[split + 1 :]
    if not command:
        p.error("no command after --")

    jobs = Jobs(args.db, args.app)
    if args.describe or args.schedule:
        jobs.define(args.name, args.describe, args.schedule, args.launchd_label, args.log)
    if jobs.paused(args.name):
        print(f"job-run: {args.name} is paused (jobs page); skipped")
        return 0
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except OSError as e:
        run_id = jobs.start(args.name)
        jobs.finish(run_id, 127, error=str(e))
        print(f"job-run: {e}", file=sys.stderr)
        return 127
    run_id = jobs.start(args.name, proc.pid)
    out, err = [""], [""]
    threads = [
        threading.Thread(target=tee, args=(proc.stdout, sys.stdout, out)),
        threading.Thread(target=tee, args=(proc.stderr, sys.stderr, err)),
    ]
    for t in threads:
        t.start()
    code = proc.wait()
    for t in threads:
        t.join()
    jobs.finish(run_id, code, out[0], err[0] if code else "")
    return code


if __name__ == "__main__":
    sys.exit(main())
