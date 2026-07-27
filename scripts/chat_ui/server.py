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
import shutil
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Tool presets the UI offers. Empty string disables every tool, which also
# removes the permission prompts a headless run cannot answer.
TOOL_PRESETS = {
    "none": "",
    "read": "Read,Glob,Grep,WebFetch,WebSearch",
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


class Backend:
    """Everything that shells out. Kept apart from the HTTP plumbing."""

    def __init__(self, cwd: Path):
        self.claude = find_executable("claude")
        self.cswap = find_executable("cswap")
        self.cwd = cwd

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

    def chat(self, message: str, session_id: str | None, tools: str) -> queue.Queue:
        """Start a turn; returns a queue of (event_name, payload) tuples.

        ``None`` is pushed when the turn is over. The prompt goes in over stdin
        rather than argv so quotes and newlines in the message need no escaping.
        """
        events: queue.Queue = queue.Queue()
        args = [
            self.claude,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--tools", TOOL_PRESETS.get(tools, ""),
        ]
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
                )
            except OSError as e:
                events.put(("error", {"message": f"Start fehlgeschlagen: {e}"}))
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
                    self._translate(msg, events)
                proc.wait(timeout=30)
                drainer.join(timeout=5)
                if proc.returncode != 0:
                    detail = b"".join(stderr_chunks).decode("utf-8", "replace")
                    events.put((
                        "error",
                        {"message": detail.strip() or f"claude endete mit {proc.returncode}"},
                    ))
            except Exception as e:  # pragma: no cover - defensive
                events.put(("error", {"message": f"{type(e).__name__}: {e}"}))
            finally:
                events.put(None)

        events.put(("session", {"sessionId": session_id}))
        threading.Thread(target=pump, daemon=True).start()
        return events

    @staticmethod
    def _translate(msg: dict, events: queue.Queue) -> None:
        """Map one stream-json message onto a UI event. Unknown kinds are dropped."""
        kind = msg.get("type")
        if kind == "assistant":
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
        elif self.path == "/api/context":
            self._json({"cwd": str(self.backend.cwd)})
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        if self.path == "/api/switch":
            target = str(self._body().get("account", "")).strip()
            if not target:
                self._json({"error": "kein Konto angegeben"}, 400)
                return
            self._json(self.backend.switch(target))
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

        events = self.backend.chat(
            message, body.get("sessionId") or None, str(body.get("tools") or "none")
        )
        while True:
            item = events.get()
            if item is None:
                break
            name, payload = item
            chunk = f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return  # browser navigated away mid-turn
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
