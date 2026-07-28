"""A local chat UI over the Claude Code CLI, with one-click account switching.

Runs `claude -p --output-format stream-json` as a child process and streams its
output to the browser over SSE, while `cswap` supplies the account list, live
quota, and the switch itself. The point is the combination: the conversation is
a local Claude Code session, so switching the paying account mid-conversation
works -- `.credentials.json` is the source of truth for the CLI, and Claude Code
re-reads it on the next turn.

    uv run python scripts/chat_ui/server.py
    uv run python scripts/chat_ui/server.py --port 8080 --cwd C:\\path\\to\\project

Binds 127.0.0.1 only. Standard library only -- no dependencies to install.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent

# Everything below is an allowlist, not a suggestion: these values reach a
# child process's argv, so the browser must never be able to name a flag or a
# value we did not choose. Unknown keys fall back to the safe default rather
# than being passed through.

# Empty string disables every tool, which also removes the permission prompts a
# headless run cannot answer. "default" hands over the full built-in set.
TOOL_PRESETS = {
    "none": "",
    "read": "Read,Glob,Grep,WebFetch,WebSearch",
    "all": "default",
}

# Aliases resolve to the current model of that family; see `claude --model`.
MODELS = ("opus", "sonnet", "haiku", "fable")

# `claude --permission-mode`. bypassPermissions is deliberately not offered:
# it removes every guard rail at once, and "dontAsk" already covers the
# unattended case the UI needs.
PERMISSION_MODES = ("plan", "acceptEdits", "dontAsk", "auto", "manual")

EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Slash commands work in print mode -- `/context` returns a real report, not an
# echo -- so the composer offers them. The authoritative list per directory
# comes from the CLI itself, in the `slash_commands` field of its init event,
# and is cached once a turn has run there. Until then this seed is offered: the
# built-ins observed in a real init event on this machine. It is deliberately
# short, because guessing at commands that may not exist is worse than a list
# that fills itself in after the first message.
SEED_COMMANDS = (
    "clear", "compact", "context", "usage", "cost", "model", "effort",
    "config", "mcp", "agents", "init", "review", "doctor", "recap", "rename",
)

# Only for commands whose effect is not in doubt. A command without an entry is
# listed without a description rather than with an invented one.
COMMAND_HELP = {
    "agents": "Unteragenten verwalten",
    "clear": "Gespräch zurücksetzen",
    "compact": "Verlauf zusammenfassen",
    "config": "Einstellungen anzeigen oder ändern",
    "context": "Kontextauslastung anzeigen",
    "cost": "Kosten dieser Sitzung",
    "doctor": "Installation prüfen",
    "effort": "Denktiefe setzen",
    "init": "CLAUDE.md für dieses Projekt anlegen",
    "mcp": "MCP-Server verwalten",
    "model": "Modell wechseln",
    "recap": "Zusammenfassung des Gesprächs",
    "rename": "Sitzung umbenennen",
    "review": "Pull Request prüfen",
    "security-review": "Sicherheitsprüfung der Änderungen",
    "usage": "Nutzung und Limits anzeigen",
}


def find_executable(name: str) -> str:
    """Locate a CLI, falling back to the install locations used on Windows.

    ``shutil.which`` resolves ``claude`` to ``claude.CMD`` and ``cswap`` to the
    uv tool shim when the parent shell's PATH carries them; a GUI-launched
    Python may not inherit either.
    """
    found = shutil.which(name)
    if found:
        return found
    candidates = [
        Path.home() / ".local" / "bin" / f"{name}.exe",
        Path(os.environ.get("APPDATA", "")) / "npm" / f"{name}.cmd",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise SystemExit(
        f"'{name}' nicht gefunden. Starte den Server aus einer Shell, in der "
        f"'{name}' funktioniert."
    )


class Turn:
    """One in-flight `claude` run: its event queue, and the means to end it.

    A turn that nobody is reading any more still costs quota until the child
    finishes, so the streaming handler cancels on a dead client. Cancellation
    races the process start, hence the lock and the ``attach`` handshake: a
    cancel arriving first sets the flag, and ``attach`` then reports False so
    the caller kills the child it just created.
    """

    def __init__(self) -> None:
        self.events: queue.Queue = queue.Queue()
        self.cancelled = False
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def attach(self, proc: subprocess.Popen) -> bool:
        """Register the child. False means the turn was already cancelled."""
        with self._lock:
            if self.cancelled:
                return False
            self._proc = proc
            return True

    def cancel(self) -> None:
        """End the run and everything it spawned.

        Killing the direct child is not enough. On Windows ``claude`` resolves
        to ``claude.CMD``, so Python launches ``cmd.exe /c claude.CMD …`` and the
        process doing the work -- and burning the quota -- is a *grandchild*.
        ``proc.kill()`` reaps the wrapper and leaves that one running to
        completion, which is precisely the cost this is meant to avoid. So kill
        the tree: ``taskkill /T`` on Windows, the process group on POSIX (the
        child is started in its own session for exactly this).
        """
        with self._lock:
            self.cancelled = True
            proc = self._proc
        if not proc or proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            try:
                proc.kill()   # belt: the wrapper itself, if taskkill missed it
            except OSError:
                pass


class Backend:
    """Everything that shells out. Kept apart from the HTTP plumbing."""

    def __init__(self, cwd: Path):
        self.claude = find_executable("claude")
        self.cswap = find_executable("cswap")
        self.cwd = cwd
        # Slash commands the CLI reported per directory, learned from init
        # events. Project and plugin commands differ by directory, so this is
        # keyed by cwd rather than kept as one global list.
        self._commands: dict[str, list[str]] = {}

    def commands(self) -> dict:
        """Slash commands offered for the current directory.

        ``exact`` is False while only the seed list is known -- no turn has run
        here yet, so the CLI has not had a chance to report its own.
        """
        known = self._commands.get(str(self.cwd))
        names = known if known is not None else list(SEED_COMMANDS)
        return {
            "commands": [
                {"name": n, "description": COMMAND_HELP.get(n, "")}
                for n in names
                if not n.startswith("__")   # internal plumbing, not for humans
            ],
            "exact": known is not None,
        }

    def set_cwd(self, raw: str) -> dict:
        """Point new turns at another directory.

        Claude Code files conversations under the directory they ran in, so a
        session started elsewhere cannot be resumed from here. The caller drops
        its session id on success — the UI turns a directory change into a new
        conversation rather than letting a resume fail mid-turn.
        """
        try:
            target = Path(raw).expanduser().resolve()
        except (OSError, ValueError) as e:
            return {"error": f"Ungueltiger Pfad: {e}"}
        if not target.is_dir():
            return {"error": f"Kein Verzeichnis: {target}"}
        self.cwd = target
        return {"cwd": str(target)}

    # -- conversation history ---------------------------------------------
    #
    # Claude Code already persists every conversation as JSONL under
    # ~/.claude/projects/<encoded cwd>/<session id>.jsonl, and `--resume` reads
    # it back. Reading that instead of keeping a second store means history
    # survives restarts for free, sessions started in a terminal show up here
    # too, and resuming is the CLI's own mechanism rather than a replay.

    @staticmethod
    def _encode_cwd(path: Path) -> str:
        """Claude Code's directory key: separators, colon and spaces become '-'."""
        out = str(path)
        for ch in (":", "\\", "/", " "):
            out = out.replace(ch, "-")
        return out

    def _project_dir(self) -> Path | None:
        """The transcript directory for the current cwd, or None if there is none.

        The encoded name is derived, then verified; if it is absent, fall back
        to reading each candidate's own recorded ``cwd`` so an encoding rule we
        got wrong degrades to a slower lookup rather than an empty list.
        """
        root = Path.home() / ".claude" / "projects"
        guess = root / self._encode_cwd(self.cwd)
        if guess.is_dir():
            return guess
        if not root.is_dir():
            return None
        target = str(self.cwd).lower()
        for candidate in root.iterdir():
            if not candidate.is_dir():
                continue
            for f in candidate.glob("*.jsonl"):
                try:
                    with f.open("r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            entry = json.loads(line)
                            recorded = entry.get("cwd")
                            if isinstance(recorded, str):
                                if recorded.lower() == target:
                                    return candidate
                                break
                except (OSError, json.JSONDecodeError):
                    pass
                break
        return None

    @staticmethod
    def _text_of(content) -> str:
        """Flatten a message's content to plain text; tool blocks are dropped."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        return ""

    def _scan_file(self, path: Path) -> dict | None:
        """Metadata for one transcript, or None when it holds no exchange.

        Byte-level prefilter before any parsing: a long conversation is mostly
        assistant lines, and each can be tens of kilobytes. Scanning every
        project directory would crawl if all of them were parsed, so only lines
        that look like a title or a user turn are decoded -- the parse then
        confirms it, so the cheap check never decides anything on its own.
        """
        try:
            stat = path.stat()
        except OSError:
            return None
        title, first_user, recorded_cwd, turns = None, None, None, 0
        try:
            with path.open("rb") as fh:
                for raw in fh:
                    if b'"aiTitle"' in raw:
                        try:
                            e = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if e.get("type") == "ai-title" and e.get("aiTitle"):
                            title = str(e["aiTitle"])
                        continue
                    if b'"type":"user"' not in raw:
                        continue
                    try:
                        e = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if e.get("type") != "user" or e.get("isSidechain"):
                        continue
                    if recorded_cwd is None and isinstance(e.get("cwd"), str):
                        recorded_cwd = e["cwd"]
                    # Tool results come back as user-role entries too. Counting
                    # those would report a tool-heavy conversation as hundreds
                    # of questions; only entries carrying actual text are ones
                    # the person typed.
                    text = self._text_of(e.get("message", {}).get("content"))
                    if not text.strip():
                        continue
                    turns += 1
                    if first_user is None:
                        first_user = text
        except OSError:
            return None
        if not turns:
            return None  # a session file with no exchange is noise in the list
        if not title:
            # First line of the opening question, while Claude Code has not
            # written its generated title yet. Skip machinery the harness wraps
            # around a prompt (<local-command-caveat>, <command-name>, ...) --
            # it is never what the conversation was about.
            lines = [
                ln.strip() for ln in (first_user or "").splitlines()
                if ln.strip() and not ln.lstrip().startswith("<")
            ]
            title = lines[0] if lines else ""
        return {
            "id": path.stem,
            "title": title.strip()[:90] or "Ohne Titel",
            "turns": turns,
            "updatedAt": stat.st_mtime,
            "cwd": recorded_cwd or "",
        }

    def list_sessions(self, scope: str = "dir", limit: int = 200) -> dict:
        """Conversations newest first, for this directory or for all of them.

        ``scope="all"`` walks every project directory, which is what makes a
        conversation findable without knowing where it was started. Each row
        carries the directory recorded inside the transcript itself, so opening
        one can move the session there -- ``--resume`` resolves an id relative
        to the working directory and would not find it otherwise.
        """
        folders: list[Path] = []
        if scope == "all":
            root = Path.home() / ".claude" / "projects"
            if root.is_dir():
                folders = [p for p in root.iterdir() if p.is_dir()]
        else:
            folder = self._project_dir()
            if folder is not None:
                folders = [folder]

        rows = []
        for folder in folders:
            try:
                files = list(folder.glob("*.jsonl"))
            except OSError:
                continue
            for f in files:
                row = self._scan_file(f)
                if row:
                    rows.append(row)
        rows.sort(key=lambda r: r["updatedAt"], reverse=True)
        return {"sessions": rows[:limit], "scope": scope}

    def read_session(self, session_id: str, limit: int = 80) -> dict:
        """The visible exchange of one conversation, newest ``limit`` messages.

        Long conversations are truncated from the front on purpose: this UI
        renders markdown per message, and a transcript with hundreds of turns
        takes seconds to lay out. ``limit=0`` loads everything, which is the
        caller's explicit choice rather than the default.
        """
        if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id):
            return {"error": "ungueltige Sitzungs-ID"}
        folder = self._project_dir()
        if folder is None:
            return {"error": "kein Verlauf fuer dieses Verzeichnis"}
        path = folder / f"{session_id}.jsonl"
        if not path.is_file():
            return {"error": "Sitzung nicht gefunden"}
        messages = []
        try:
            with path.open("rb") as fh:
                for raw in fh:
                    if b'"type":"user"' not in raw and b'"type":"assistant"' not in raw:
                        continue
                    try:
                        e = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if e.get("isSidechain"):
                        continue          # subagent traffic, not the conversation
                    kind = e.get("type")
                    if kind not in ("user", "assistant"):
                        continue
                    text = self._text_of(e.get("message", {}).get("content"))
                    if text.strip():
                        messages.append({"role": kind, "text": text})
        except OSError as e:
            return {"error": str(e)}
        total = len(messages)
        truncated = bool(limit) and total > limit
        if truncated:
            messages = messages[-limit:]
        return {
            "id": session_id,
            "messages": messages,
            "total": total,
            "truncated": truncated,
        }

    def list_dirs(self) -> dict:
        """The current directory, its parent, and its immediate subdirectories."""
        try:
            subdirs = sorted(
                (p.name for p in self.cwd.iterdir() if p.is_dir() and not p.name.startswith(".")),
                key=str.lower,
            )
        except OSError as e:
            subdirs = []
            _ = e
        parent = self.cwd.parent
        return {
            "cwd": str(self.cwd),
            "parent": str(parent) if parent != self.cwd else None,
            "subdirs": subdirs[:200],
        }

    def _run(self, args: list[str], timeout: int = 60) -> tuple[int, str, str]:
        """Run a command, decoding output as UTF-8 explicitly.

        Never `text=True`: on a German Windows the child's output is decoded
        with the ANSI code page, which raises on bytes these CLIs legitimately
        emit and leaves stdout as None.
        """
        proc = subprocess.run(
            args, capture_output=True, timeout=timeout, cwd=str(self.cwd)
        )
        return (
            proc.returncode,
            (proc.stdout or b"").decode("utf-8", "replace"),
            (proc.stderr or b"").decode("utf-8", "replace"),
        )

    def accounts(self) -> dict:
        rc, out, err = self._run([self.cswap, "list", "--json"])
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"error": (err or out or f"cswap list failed (rc={rc})").strip()}

    def switch(self, target: str) -> dict:
        rc, out, err = self._run([self.cswap, "switch", target, "--json"])
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return {"error": (err or out or f"cswap switch failed (rc={rc})").strip()}
        return data

    def chat(self, message: str, session_id: str | None, opts: dict) -> "Turn":
        """Start a turn. The returned handle carries its event queue and a kill.

        ``None`` is pushed onto the queue when the turn is over. The prompt goes
        in over stdin rather than argv so quotes and newlines in the message need
        no escaping. Every option is checked against its allowlist before it
        reaches argv.

        The handle matters for cost: if nobody is listening any more -- the tab
        closed, the user pressed stop -- the child must be killed, or it runs the
        turn to completion and bills an answer no one will read.
        """
        turn = Turn()
        events = turn.events
        args = [
            self.claude,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--tools", TOOL_PRESETS.get(str(opts.get("tools")), ""),
        ]
        model = str(opts.get("model") or "")
        if model in MODELS:
            args += ["--model", model]
        mode = str(opts.get("permissionMode") or "")
        if mode in PERMISSION_MODES:
            args += ["--permission-mode", mode]
        effort = str(opts.get("effort") or "")
        if effort in EFFORTS:
            args += ["--effort", effort]

        if session_id:
            args += ["--resume", session_id]
        else:
            session_id = str(uuid.uuid4())
            args += ["--session-id", session_id]

        def pump() -> None:
            try:
                proc = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(self.cwd),
                    # POSIX: own session, so cancelling can signal the whole
                    # group. Windows gets the same reach via taskkill /T.
                    start_new_session=(sys.platform != "win32"),
                )
            except OSError as e:
                events.put(("error", {"message": f"Start fehlgeschlagen: {e}"}))
                events.put(None)
                return
            if not turn.attach(proc):
                # Cancelled between the request arriving and the child starting.
                proc.kill()
                events.put(None)
                return
            # stderr must be drained concurrently. Reading only stdout lets a
            # chatty stderr fill its pipe buffer, at which point the child
            # blocks writing to it and never produces another stdout line --
            # the turn hangs with no output at all.
            stderr_chunks: list[bytes] = []

            def drain() -> None:
                try:
                    for chunk in iter(lambda: proc.stderr.read(4096), b""):
                        stderr_chunks.append(chunk)
                except (OSError, ValueError):
                    pass

            drainer = threading.Thread(target=drain, daemon=True)
            drainer.start()

            try:
                proc.stdin.write(message.encode("utf-8"))
                proc.stdin.close()
                for raw in proc.stdout:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("type") == "system" and msg.get("subtype") == "init":
                        # The CLI names its own commands here; nothing else has
                        # an authoritative list for this directory.
                        found = msg.get("slash_commands")
                        if isinstance(found, list):
                            self._commands[str(self.cwd)] = [str(c) for c in found]
                    self._translate(msg, events)
                proc.wait(timeout=30)
                drainer.join(timeout=5)
                if proc.returncode != 0 and not turn.cancelled:
                    detail = b"".join(stderr_chunks).decode("utf-8", "replace")
                    events.put((
                        "error",
                        {"message": detail.strip() or f"claude endete mit {proc.returncode}"},
                    ))
            except Exception as e:  # pragma: no cover - defensive
                if not turn.cancelled:
                    events.put(("error", {"message": f"{type(e).__name__}: {e}"}))
            finally:
                events.put(None)

        events.put(("session", {"sessionId": session_id}))
        threading.Thread(target=pump, daemon=True).start()
        return turn

    @staticmethod
    def _translate(msg: dict, events: queue.Queue) -> None:
        """Map one stream-json message onto a UI event. Unknown kinds are dropped."""
        kind = msg.get("type")
        if kind == "system" and msg.get("subtype") == "init":
            # What the CLI actually resolved, rather than what we asked for --
            # the UI shows this so a rejected or aliased option is visible.
            events.put((
                "info",
                {"model": msg.get("model"), "permissionMode": msg.get("permissionMode")},
            ))
        elif kind == "assistant":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    events.put(("text", {"text": block["text"]}))
                elif block.get("type") == "tool_use":
                    events.put(("tool", {"name": block.get("name", "?")}))
        elif kind == "rate_limit_event":
            # The CLI reports the live limit state per turn -- worth surfacing,
            # it is fresher than the polled usage numbers in the sidebar.
            events.put(("limit", msg.get("rate_limit_info", {})))
        elif kind == "result":
            events.put((
                "done",
                {
                    "cost": msg.get("total_cost_usd"),
                    "durationMs": msg.get("duration_ms"),
                    "isError": bool(msg.get("is_error")),
                    "result": msg.get("result"),
                },
            ))


class Handler(BaseHTTPRequestHandler):
    backend: Backend  # injected below

    def log_message(self, fmt, *args):  # quieter console
        pass

    # -- helpers ---------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            try:
                html = (HERE / "index.html").read_bytes()
            except OSError as e:
                self._send(500, str(e).encode(), "text/plain; charset=utf-8")
                return
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path == "/api/accounts":
            self._json(self.backend.accounts())
        elif self.path == "/api/commands":
            self._json(self.backend.commands())
        elif self.path.split("?")[0] == "/api/sessions":
            q = parse_qs(urlparse(self.path).query)
            scope = "all" if q.get("scope", ["dir"])[0] == "all" else "dir"
            self._json(self.backend.list_sessions(scope))
        elif self.path.split("?")[0] == "/api/session":
            q = parse_qs(urlparse(self.path).query)
            wanted = q.get("id", [""])[0]
            try:
                limit = max(0, min(2000, int(q.get("limit", ["80"])[0])))
            except ValueError:
                limit = 80
            self._json(self.backend.read_session(wanted, limit))
        elif self.path == "/api/context":
            # Options ship with the context so the UI never hardcodes a value
            # the server would reject.
            self._json({
                **self.backend.list_dirs(),
                "models": list(MODELS),
                "permissionModes": list(PERMISSION_MODES),
                "efforts": list(EFFORTS),
                "toolPresets": list(TOOL_PRESETS),
            })
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        if self.path == "/api/switch":
            target = str(self._body().get("account", "")).strip()
            if not target:
                self._json({"error": "kein Konto angegeben"}, 400)
                return
            self._json(self.backend.switch(target))
        elif self.path == "/api/cwd":
            raw = str(self._body().get("cwd", "")).strip()
            if not raw:
                self._json({"error": "kein Pfad angegeben"}, 400)
                return
            result = self.backend.set_cwd(raw)
            if "error" in result:
                self._json(result, 400)
                return
            self._json(self.backend.list_dirs())
        elif self.path == "/api/chat":
            self._stream_chat()
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def _stream_chat(self) -> None:
        body = self._body()
        message = str(body.get("message", "")).strip()
        if not message:
            self._json({"error": "leere Nachricht"}, 400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # HTTP/1.0 with no Content-Length: the client reads until we close, so
        # the connection must actually close. Announcing keep-alive here would
        # leave it waiting for bytes that never come after the last event.
        self.send_header("Connection", "close")
        self.end_headers()

        turn = self.backend.chat(message, body.get("sessionId") or None, body)
        while True:
            item = turn.events.get()
            if item is None:
                break
            name, payload = item
            chunk = f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # Tab closed or stop pressed. Killing the child here is the
                # whole point: otherwise it finishes the turn and bills an
                # answer nobody will see.
                turn.cancel()
                return
        try:
            self.wfile.write(b"event: end\ndata: {}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--cwd",
        default=os.getcwd(),
        help="Arbeitsverzeichnis der Claude-Sitzung (Standard: aktuelles)",
    )
    args = parser.parse_args()

    cwd = Path(args.cwd).resolve()
    if not cwd.is_dir():
        raise SystemExit(f"Kein Verzeichnis: {cwd}")

    Handler.backend = Backend(cwd)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Chat-UI laeuft auf {url}")
    print(f"  Arbeitsverzeichnis : {cwd}")
    print(f"  claude             : {Handler.backend.claude}")
    print(f"  cswap              : {Handler.backend.cswap}")
    print("Beenden mit Strg+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbeendet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
