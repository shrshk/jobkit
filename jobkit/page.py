"""job-page: the jobs page for an app that has no web server of its own (only shell jobs recorded by job-run).

    job-page --db ~/Library/Application\\ Support/app/jobs.db --app "My App" --port 8089

Serves GET /jobs (the shared page), GET /api/jobs, and the page's Run now and Pause. Listens on 127.0.0.1 unless
--host says otherwise; put it behind the app's proxy at /jobs and /api/jobs, and name the proxy's host name with
--allow-host (repeatable). Any other Host is refused (421): a web page that points its own name at this machine
(DNS rebinding) would otherwise read the runs and press the buttons.
"""

import argparse
import json
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from jobkit import Jobs, page_html

ACTION = re.compile(r"^/api/jobs/([^/]+)/(run|pause)$")
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
MAX_BODY = 4096  # a pause is {"paused": true}
# Never inside another site's frame (a click on Pause or Run now made under a decoy), never sniffed as another type.
SAFETY = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
}


def known_host(host: str, allowed: set[str]) -> bool:
    """The Host header names this machine (loopback) or one of the proxy's names given with --allow-host."""
    try:
        name = urlsplit("//" + host).hostname or ""
    except ValueError:
        return False
    return name in LOOPBACK or name in allowed


def handler(jobs: Jobs, app: str, allow_hosts: tuple[str, ...] = ()) -> type[BaseHTTPRequestHandler]:
    page = page_html(app).encode()
    allowed = {h.lower() for h in allow_hosts}

    class Handler(BaseHTTPRequestHandler):
        timeout = 30  # a client that stops sending does not hold a thread for good

        def send(self, status: int, body: bytes = b"", kind: str = "application/json") -> None:
            self.send_response(status)
            for k, v in SAFETY.items():
                self.send_header(k, v)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def refused(self) -> bool:
            if known_host(self.headers.get("Host") or "", allowed):
                return False
            self.send(421, json.dumps({"detail": "unknown host name (job-page --allow-host)"}).encode())
            return True

        def do_GET(self):
            if self.refused():
                return
            path = self.path.split("?")[0]
            if path in ("/jobs", "/jobs/"):
                self.send(200, page, "text/html; charset=utf-8")
            elif path == "/api/jobs":
                self.send(200, json.dumps({"app": app, "jobs": jobs.report()}).encode())
            else:
                self.send(404)

        def do_POST(self):
            if self.refused():
                return
            m = ACTION.match(self.path.split("?")[0])
            if not m:
                return self.send(404)
            # Only JSON: a browser sends it cross-site only after a CORS preflight, which this server never answers,
            # so another site's page cannot run or pause a job (CSRF). The jobs page always sends JSON.
            if self.headers.get_content_type() != "application/json":
                return self.send(415, json.dumps({"detail": "send application/json"}).encode())
            name = unquote(m[1])
            try:
                size = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                size = -1
            if not 0 <= size <= MAX_BODY:
                return self.send(400, json.dumps({"detail": "bad body size"}).encode())
            try:
                body = json.loads(self.rfile.read(size) or b"{}")
                paused = body.get("paused", True) if isinstance(body, dict) else None
                if m[2] == "run":
                    jobs.run_now(name)
                    return self.send(202)
                if not isinstance(paused, bool):
                    return self.send(400, json.dumps({"detail": 'send {"paused": true} or {"paused": false}'}).encode())
                jobs.pause(name, paused)
                return self.send(204)
            except LookupError:
                return self.send(404, json.dumps({"detail": "no such job, or nothing to start"}).encode())
            except ValueError:
                return self.send(400, json.dumps({"detail": "not JSON"}).encode())
            except subprocess.CalledProcessError:
                return self.send(502, json.dumps({"detail": "launchd did not start the job"}).encode())

        def log_message(self, *args):  # quiet: the app's own log has the runs
            pass

    return Handler


def serve(
    db: str, app: str, host: str = "127.0.0.1", port: int = 8089, allow_hosts: tuple[str, ...] = ()
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), handler(Jobs(db, app), app, allow_hosts))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="job-page")
    p.add_argument("--db", required=True)
    p.add_argument("--app", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8089)
    p.add_argument(
        "--allow-host",
        dest="allow_hosts",
        action="append",
        default=[],
        help="a host name the proxy serves the page under",
    )
    a = p.parse_args(argv)
    serve(a.db, a.app, a.host, a.port, tuple(a.allow_hosts)).serve_forever()
