"""Tests for the local chat UI server.

It had none. 2400 lines of server and page went in unguarded, and every defect
in it was found by hand or by the user: a stderr deadlock that hung a turn with
no output, a cancel that killed a wrapper and left the grandchild burning quota,
titles reduced to a single letter, question counts inflated threefold by tool
results, and a cross-origin write that any open web page could have fired.

The script lives outside the package, so it is loaded by path.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest

SERVER_PY = Path(__file__).resolve().parents[1] / "scripts" / "chat_ui" / "server.py"


@pytest.fixture(scope="module")
def srv():
    """The server module, imported without running main()."""
    spec = importlib.util.spec_from_file_location("chat_ui_server", SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def backend(srv, tmp_path, monkeypatch):
    """A Backend that never looks for the real CLIs."""
    monkeypatch.setattr(srv, "find_executable", lambda name: f"/fake/{name}")
    b = srv.Backend(tmp_path)
    monkeypatch.setattr(type(b), "UPLOAD_DIR", tmp_path / "uploads")
    return b


# -- upload name sanitising -------------------------------------------------
#
# The name comes from the browser, so it is attacker-controlled in the CSRF
# sense as well as the clumsy-filename sense.

@pytest.mark.parametrize("given", [
    "../../../etc/passwd",
    r"..\..\windows\system32\evil.dll",
    "C:/absolute/path.txt",
    "/etc/shadow",
    "....//....//x",
    "con.txt",
    "",
    "   ",
    "x" * 300 + ".txt",
    "mit leerzeichen & zeichen!.md",
])
def test_upload_stays_inside_its_directory(backend, given):
    result = backend.save_upload(given, b"inhalt")
    target = Path(result["path"]).resolve()
    assert target.is_relative_to(backend.UPLOAD_DIR.resolve())
    assert target.is_file()
    # No separator survives into the stored name.
    assert not {"/", "\\"} & set(target.name)


def test_upload_keeps_content_byte_for_byte(backend):
    data = bytes(range(256))
    saved = Path(backend.save_upload("rohdaten.bin", data)["path"])
    assert saved.read_bytes() == data


def test_upload_names_do_not_collide(backend):
    a = backend.save_upload("bild.png", b"a")["path"]
    b = backend.save_upload("bild.png", b"b")["path"]
    assert a != b
    assert Path(a).read_bytes() == b"a" and Path(b).read_bytes() == b"b"


def test_uploads_info_and_clear(backend):
    for i in range(3):
        backend.save_upload(f"f{i}.txt", b"1234")
    info = backend.uploads_info()
    assert info["count"] == 3 and info["bytes"] == 12
    cleared = backend.clear_uploads()
    assert cleared["removed"] == 3 and cleared["count"] == 0


def test_clear_uploads_leaves_subdirectories_alone(backend):
    backend.save_upload("a.txt", b"x")
    (backend.UPLOAD_DIR / "unterordner").mkdir()
    backend.clear_uploads()
    assert (backend.UPLOAD_DIR / "unterordner").is_dir()


# -- option allowlists ------------------------------------------------------
#
# These values reach a child process's argv.

def test_only_allowlisted_options_reach_argv(srv, backend, monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, args, **kw):
            seen["args"] = args
            seen["cwd"] = kw.get("cwd")
            self.stdin = _Sink()
            self.stdout = iter(())
            self.stderr = _Sink()
            self.returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(srv.subprocess, "Popen", FakePopen)
    turn = backend.chat("hallo", None, {
        "model": "--append-system-prompt",      # a real flag, not an allowed model
        "permissionMode": "erfunden",
        "effort": "; whoami",
        "tools": "gibtsnicht",
    })
    while turn.events.get() is not None:
        pass
    args = seen["args"]
    assert "--append-system-prompt" not in args
    assert "erfunden" not in args and "; whoami" not in args
    assert "--model" not in args and "--permission-mode" not in args
    # An unknown tool preset falls back to "no tools", not to everything.
    assert args[args.index("--tools") + 1] == ""


def test_allowlisted_options_do_reach_argv(srv, backend, monkeypatch):
    seen = {}
    monkeypatch.setattr(srv.subprocess, "Popen", _recorder(seen))
    turn = backend.chat("hallo", None, {
        "model": "haiku", "permissionMode": "plan", "effort": "low", "tools": "read",
    })
    while turn.events.get() is not None:
        pass
    args = seen["args"]
    assert args[args.index("--model") + 1] == "haiku"
    assert args[args.index("--permission-mode") + 1] == "plan"
    assert args[args.index("--effort") + 1] == "low"
    assert "Read" in args[args.index("--tools") + 1]


def _recorder(seen: dict):
    """A Popen stand-in that records how it was called and behaves like a
    finished process. Not a lambda: `seen.setdefault(...) or _FakeProc()`
    returned the recorded value, so no process object was ever produced."""
    def popen(args, **kw):
        seen["args"] = args
        seen["cwd"] = kw.get("cwd")
        return _FakeProc()
    return popen


class _Sink:
    def write(self, *_):
        pass

    def close(self):
        pass

    def read(self, *_):
        return b""


class _FakeProc:
    def __init__(self):
        self.stdin = _Sink()
        self.stdout = iter(())
        self.stderr = _Sink()
        self.returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


# -- per-request working directory ------------------------------------------

def test_request_directory_overrides_the_startup_one(backend, tmp_path):
    other = tmp_path / "anderes"
    other.mkdir()
    assert backend.resolve_cwd(str(other)) == other.resolve()


@pytest.mark.parametrize("bad", [None, "", "   ", 42, "C:/gibt/es/nicht/wirklich"])
def test_unusable_request_directory_falls_back(backend, bad):
    assert backend.resolve_cwd(bad) == backend.cwd


def test_chat_runs_in_the_requested_directory(srv, backend, tmp_path, monkeypatch):
    other = tmp_path / "projekt-b"
    other.mkdir()
    seen = {}
    monkeypatch.setattr(srv.subprocess, "Popen", _recorder(seen))
    turn = backend.chat("hallo", None, {"cwd": str(other)})
    while turn.events.get() is not None:
        pass
    assert Path(seen["cwd"]) == other.resolve()


# -- transcripts ------------------------------------------------------------

def write_transcript(folder: Path, sid: str, entries: list[dict]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries), encoding="utf-8")


def user_entry(text, cwd="C:/projekt", sidechain=False):
    return {"type": "user", "isSidechain": sidechain, "cwd": cwd,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def tool_result_entry(cwd="C:/projekt"):
    """A tool result is also a user-role entry, and must not count as a question."""
    return {"type": "user", "cwd": cwd,
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "content": "ok"}]}}


def assistant_entry(text):
    return {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


@pytest.fixture
def project(backend, tmp_path, monkeypatch):
    """A fake ~/.claude/projects tree wired to the backend's cwd."""
    root = tmp_path / "home" / ".claude" / "projects"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    folder = root / backend._encode_cwd(backend.cwd)
    folder.mkdir(parents=True)
    return folder


def test_tool_results_are_not_counted_as_questions(backend, project):
    write_transcript(project, "1" * 8 + "-aaaa", [
        user_entry("echte Frage", cwd=str(backend.cwd)),
        *[tool_result_entry(cwd=str(backend.cwd)) for _ in range(30)],
        assistant_entry("Antwort"),
    ])
    row = backend.list_sessions()["sessions"][0]
    assert row["turns"] == 1, "Werkzeug-Rueckgaben duerfen nicht als Fragen zaehlen"


def test_title_uses_the_generated_one_whole(backend, project):
    write_transcript(project, "2" * 8 + "-bbbb", [
        user_entry("irgendwas", cwd=str(backend.cwd)),
        {"type": "ai-title", "aiTitle": "Setup dual account switcher"},
    ])
    row = backend.list_sessions()["sessions"][0]
    assert row["title"] == "Setup dual account switcher", "nicht nur das erste Zeichen"


def test_title_skips_harness_markup(backend, project):
    write_transcript(project, "3" * 8 + "-cccc", [
        user_entry("<local-command-caveat>Caveat: ...</local-command-caveat>\n"
                   "Die echte Frage", cwd=str(backend.cwd)),
    ])
    assert backend.list_sessions()["sessions"][0]["title"] == "Die echte Frage"


def test_sidechain_entries_are_ignored(backend, project):
    write_transcript(project, "4" * 8 + "-dddd", [
        user_entry("Hauptfrage", cwd=str(backend.cwd)),
        user_entry("Unteragent", cwd=str(backend.cwd), sidechain=True),
    ])
    assert backend.list_sessions()["sessions"][0]["turns"] == 1


def test_empty_transcripts_are_not_listed(backend, project):
    write_transcript(project, "5" * 8 + "-eeee", [{"type": "queue-operation"}])
    assert backend.list_sessions()["sessions"] == []


def test_read_session_truncates_from_the_front(backend, project):
    sid = "6" * 8 + "-ffff"
    entries = []
    for i in range(60):
        entries.append(user_entry(f"frage {i}", cwd=str(backend.cwd)))
        entries.append(assistant_entry(f"antwort {i}"))
    write_transcript(project, sid, entries)

    full = backend.read_session(sid, 0)
    assert full["total"] == 120 and not full["truncated"]

    cut = backend.read_session(sid, 10)
    assert cut["truncated"] and len(cut["messages"]) == 10 and cut["total"] == 120
    assert "antwort 59" in cut["messages"][-1]["text"], "die neuesten muessen bleiben"


@pytest.mark.parametrize("bad", [
    "../../../etc/passwd", r"..\..\secrets", "nicht;erlaubt", "", "  ",
    "x" * 200, "/absolut",
])
def test_read_session_rejects_bad_ids(backend, project, bad):
    assert "error" in backend.read_session(bad)


# -- slash commands ---------------------------------------------------------

def test_seed_commands_until_the_cli_reports_its_own(backend):
    d = backend.commands()
    assert d["exact"] is False and d["commands"]
    assert all(not c["name"].startswith("__") for c in d["commands"])


def test_reported_commands_replace_the_seed(backend):
    backend._commands[str(backend.cwd)] = ["context", "__remote-workflow", "plugin:x"]
    d = backend.commands()
    assert d["exact"] is True
    names = [c["name"] for c in d["commands"]]
    assert "__remote-workflow" not in names, "Interna gehoeren nicht ins Menue"
    assert names == ["context", "plugin:x"]


def test_commands_are_cached_per_directory(backend, tmp_path):
    other = tmp_path / "zweitprojekt"
    other.mkdir()
    backend._commands[str(backend.cwd)] = ["context"]
    assert backend.commands(backend.cwd)["exact"] is True
    assert backend.commands(other)["exact"] is False


# -- file listing -----------------------------------------------------------

def test_file_listing_skips_heavy_and_hidden_directories(backend, tmp_path):
    for rel in ["src/app.py", "node_modules/pkg/i.js", ".git/config",
                ".venv/lib/x.py", "build/out.o", ".secret/key"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    files = backend.list_files(cwd=tmp_path)["files"]
    assert files == ["src/app.py"]


def test_file_listing_filters_and_reports_the_total(backend, tmp_path):
    for i in range(30):
        (tmp_path / f"datei{i}.txt").write_text("x", encoding="utf-8")
    (tmp_path / "besonders.md").write_text("x", encoding="utf-8")
    d = backend.list_files("besonders", cwd=tmp_path)
    assert d["files"] == ["besonders.md"] and d["total"] == 1
    all_of = backend.list_files("", limit=5, cwd=tmp_path)
    assert len(all_of["files"]) == 5 and all_of["total"] == 31


# -- the live HTTP surface --------------------------------------------------
#
# The gate that matters: a page open in the browser must not be able to spend
# quota or switch the account through this server.

@pytest.fixture
def live(srv, tmp_path, monkeypatch):
    """A running server on a free port, with the CLIs stubbed out."""
    monkeypatch.setattr(srv, "find_executable", lambda name: f"/fake/{name}")
    srv.Handler.backend = srv.Backend(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = srv.ThreadingHTTPServer(("127.0.0.1", port), srv.Handler)
    server.chat_token = "test-token"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for _ in range(50):
        try:
            http.client.HTTPConnection("127.0.0.1", port, timeout=1).connect()
            break
        except OSError:
            time.sleep(0.02)
    yield port
    server.shutdown()
    server.server_close()


def request(port, path, *, method="GET", body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, payload, headers or {})
    r = conn.getresponse()
    data = r.read().decode("utf-8", "replace")
    conn.close()
    return r.status, data


JSON_TOKEN = {"Content-Type": "application/json", "X-Chat-Token": "test-token"}


def test_page_loads_without_a_token_and_receives_one(live):
    status, body = request(live, "/")
    assert status == 200
    token = re.search(r'const CHAT_TOKEN = "([^"]+)"', body).group(1)
    assert token == "test-token", "der Platzhalter muss ersetzt werden"


def test_api_needs_the_token(live):
    assert request(live, "/api/accounts")[0] == 403
    assert request(live, "/api/accounts",
                   headers={"X-Chat-Token": "falsch"})[0] == 403
    assert request(live, "/api/accounts",
                   headers={"X-Chat-Token": "test-token"})[0] == 200


def test_transcripts_are_not_readable_without_the_token(live):
    assert request(live, "/api/sessions")[0] == 403
    assert request(live, "/api/session?id=abcdefgh")[0] == 403


@pytest.mark.parametrize("path,body", [
    ("/api/switch", {"account": "work"}),
    ("/api/cwd", {"cwd": "C:/"}),
    ("/api/chat", {"message": "hallo"}),
])
def test_writes_need_the_token(live, path, body):
    status, _ = request(live, path, method="POST", body=body,
                        headers={"Content-Type": "application/json"})
    assert status == 403


def test_the_simple_request_attack_is_refused(live):
    """text/plain triggers no CORS preflight, so it was the way in."""
    status, _ = request(live, "/api/cwd", method="POST", body={"cwd": "C:/"},
                        headers={"Content-Type": "text/plain",
                                 "Origin": "https://boese.example"})
    assert status == 403


def test_foreign_origin_is_refused_even_with_a_token(live):
    status, _ = request(live, "/api/cwd", method="POST", body={"cwd": "C:/"},
                        headers={**JSON_TOKEN, "Origin": "https://boese.example"})
    assert status == 403


def test_own_origin_is_accepted(live):
    status, _ = request(live, "/api/context", method="GET",
                        headers={"X-Chat-Token": "test-token",
                                 "Origin": f"http://127.0.0.1:{live}"})
    assert status == 200


def test_unknown_api_route_answers_readable_json(live):
    """A bare "not found" reached the browser as a JSON parse error."""
    status, body = request(live, "/api/gibtsnicht",
                           headers={"X-Chat-Token": "test-token"})
    assert status == 404
    assert "gibtsnicht" in json.loads(body)["error"]


def test_upload_rejects_an_oversized_declaration(live):
    conn = http.client.HTTPConnection("127.0.0.1", live, timeout=10)
    conn.putrequest("POST", "/api/upload?name=zugross.bin")
    conn.putheader("Content-Type", "application/octet-stream")
    conn.putheader("X-Chat-Token", "test-token")
    conn.putheader("Content-Length", str(200 * 1024 * 1024))
    conn.endheaders()
    status = conn.getresponse().status
    conn.close()
    assert status == 413, "die Grenze muss vor dem Lesen des Koerpers greifen"
