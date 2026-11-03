"""What an app needs on this Mac before it can be set up: each part checked, with what it is for and how to get it,
and an installer that runs one fixed command (an argument list, never a shell line) when the person asks. Steps only
the person can do (Homebrew's own installer, which asks for the Mac's password; signing in to Tailscale; macOS
permissions) are a link or an "after" line, re-checked until done. The app serves the list and the Install button
(only to requests from the Mac itself, see local()); parts every app needs are here, its own it adds:

    parts = [prereqs.brew(), prereqs.uv(), prereqs.tailscale(optional=True), Part("jellyfin", ...)]
    setup = prereqs.Installer(parts)
    setup.report()        # GET: [{"key", "name", "why", "ok", "detail", "install", "link", "after", ...}]
    setup.start("uv")     # POST: runs `brew install uv` in a thread; report() shows its progress and log
"""

import json
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

BREW_DIRS = ["/opt/homebrew/bin", "/usr/local/bin"]
TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"  # the Mac app does not put it on PATH


@dataclass
class Part:
    key: str
    name: str
    why: str
    check: Callable[[], tuple[bool, str]]  # (ok, what was found: "version 10.11", "not installed", ...)
    install: list[str] = field(default_factory=list)  # e.g. ["brew", "install", "uv"]; empty: link or after only
    link: str = ""
    after: str = ""  # what the person does once it is installed ("Open Tailscale and sign in.")
    optional: bool = False


def which(name: str) -> str | None:
    """On PATH, or where Homebrew puts it (launchd jobs and apps often start without Homebrew's PATH)."""
    return shutil.which(name) or shutil.which(name, path=":".join(BREW_DIRS))


def found(name: str) -> Callable[[], tuple[bool, str]]:
    def check() -> tuple[bool, str]:
        path = which(name)
        return (True, path) if path else (False, "not installed")

    return check


def brew() -> Part:
    return Part("brew", "Homebrew", "Installs the other parts.", found("brew"), link="https://brew.sh",
                after="Its installer asks for the Mac's password: paste the line from brew.sh into Terminal.")  # fmt: skip


def uv() -> Part:
    return Part("uv", "uv", "Runs the app (Python).", found("uv"), ["brew", "install", "uv"])


def rclone() -> Part:
    return Part("rclone", "rclone", "Copies the off-site backup, encrypted.", found("rclone"), ["brew", "install", "rclone"])  # fmt: skip


def tailscale_cli() -> str | None:
    return which("tailscale") or (TAILSCALE_APP if Path(TAILSCALE_APP).exists() else None)


def tailscale_status() -> dict:
    """`tailscale status --json`, or {} without Tailscale."""
    cli = tailscale_cli()
    if not cli:
        return {}
    try:
        done = subprocess.run([cli, "status", "--json"], check=False, capture_output=True, text=True, timeout=10)
        return json.loads(done.stdout or "{}") if done.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def tailscale_name() -> str:
    """This Mac's Tailscale name (mac.tail1234.ts.net), or "": the name other devices on the tailnet reach it by,
    so an app's Host check can allow it with no domain or hosts file."""
    return ((tailscale_status().get("Self") or {}).get("DNSName") or "").rstrip(".").lower()


def served() -> dict[int, str]:
    """What `tailscale serve` puts on this Mac's Tailscale name: {https port: the local address it forwards to}."""
    cli = tailscale_cli()
    if not cli:
        return {}
    try:
        done = subprocess.run(
            [cli, "serve", "status", "--json"], check=False, capture_output=True, text=True, timeout=10
        )
        status = json.loads(done.stdout or "{}") if done.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    out = {}
    for host, web in (status.get("Web") or {}).items():
        target = ((web.get("Handlers") or {}).get("/") or {}).get("Proxy", "")
        out[int(host.rsplit(":", 1)[1]) if ":" in host else 443] = target
    return out


def serve(port: int, target: str) -> str:
    """Puts the local address `target` (http://127.0.0.1:<port>) on https://<this Mac's Tailscale name>:<port>, for
    the tailnet only (never Funnel), kept across restarts; returns that address. ValueError when Tailscale is not
    there or not signed in, or the port already forwards to something else (another app's)."""
    cli, name = tailscale_cli(), tailscale_name()
    if not cli or not name:
        raise ValueError("Tailscale is not installed or not signed in")
    now = served().get(port)
    if now and now.rstrip("/") != target.rstrip("/"):
        raise ValueError(f"port {port} on Tailscale already goes to {now}: pick another port")
    done = subprocess.run(
        [cli, "serve", "--bg", f"--https={port}", target], check=False, capture_output=True, text=True, timeout=30
    )
    if done.returncode:
        raise ValueError(f"tailscale serve failed: {(done.stderr or done.stdout).strip()[:200]}")
    return f"https://{name}" + ("" if port == 443 else f":{port}")


def unserve(port: int) -> None:
    """Takes the port off this Mac's Tailscale name (nothing else that is served changes)."""
    cli = tailscale_cli()
    if cli and port in served():
        subprocess.run([cli, "serve", f"--https={port}", "off"], check=False, capture_output=True, timeout=30)


def tailscale(optional: bool = False) -> Part:
    def check() -> tuple[bool, str]:
        if not tailscale_cli():
            return False, "not installed"
        state = tailscale_status().get("BackendState")
        return (
            (True, "connected") if state == "Running" else (False, f"installed, not connected ({state or 'unknown'})")
        )

    return Part("tailscale", "Tailscale", "Reaches the app from your phone and other devices, at home or away.", check,
                ["brew", "install", "--cask", "tailscale"], after="Open Tailscale and sign in.", optional=optional)  # fmt: skip


def local(client_host: str, forwarded: bool) -> bool:
    """A request straight from this Mac (loopback, not relayed by a proxy, which adds X-Forwarded-For): the only
    one that may install software. A phone on the tailnet may look, not install."""
    return client_host in ("127.0.0.1", "::1") and not forwarded


class Installer:
    def __init__(self, parts: list[Part], runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None):
        self.parts = {p.key: p for p in parts}
        self.runner = runner or self.run
        self.state: dict[str, dict] = {}  # key -> {"running", "ok", "log"}
        self.lock = threading.Lock()

    @staticmethod
    def run(cmd: list[str]) -> subprocess.CompletedProcess:
        brew_path = which("brew")
        cmd = [brew_path, *cmd[1:]] if brew_path and cmd[0] == "brew" else cmd
        return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=1800)

    def report(self) -> list[dict]:
        out = []
        for p in self.parts.values():
            ok, detail = p.check()
            out.append(
                {
                    "key": p.key,
                    "name": p.name,
                    "why": p.why,
                    "ok": ok,
                    "detail": detail,
                    "optional": p.optional,
                    "install": " ".join(p.install),
                    "link": p.link,
                    "after": p.after,
                    "installing": dict(self.state.get(p.key, {})),
                }
            )
        return out

    def start(self, key: str) -> None:
        """Installs one missing part in a thread. LookupError: no such part, or nothing to install for it;
        ValueError (with a reason for the page): Homebrew missing, already there, already installing."""
        p = self.parts.get(key)
        if not p or not p.install:
            raise LookupError(key)
        if p.install[0] == "brew" and not which("brew"):
            raise ValueError("Install Homebrew first")
        if p.check()[0]:
            raise ValueError(f"{p.name} is already there")
        with self.lock:
            if self.state.get(key, {}).get("running"):
                raise ValueError("already installing")
            self.state[key] = {"running": True, "ok": None, "log": ""}

        def work():
            try:
                r = self.runner(p.install)
                result = {"ok": r.returncode == 0, "log": (r.stdout + r.stderr)[-2000:]}
            except (OSError, subprocess.SubprocessError) as e:
                result = {"ok": False, "log": str(e)}
            self.state[key] = {"running": False} | result

        threading.Thread(target=work, daemon=True).start()
