"""Manage and monitor one Claude desktop app profile per account.

The desktop app cannot have its account swapped by rewriting credentials -- its
token cache is downstream of the web session, and it clears and re-mints the
cache on startup. Separate Electron profiles do work: each ``--user-data-dir``
owns its cookie jar, LocalStorage and OSCrypt key, so two accounts run side by
side with independent quota.

    uv run python scripts/desktop_profiles.py list
    uv run python scripts/desktop_profiles.py create privat
    uv run python scripts/desktop_profiles.py launch privat
    uv run python scripts/desktop_profiles.py usage
    uv run python scripts/desktop_profiles.py shortcuts

No token material is ever printed.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

from claude_swap import desktop_store as ds
from claude_swap import oauth

APP_ROOT = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "AnthropicClaude"
DESKTOP = pathlib.Path.home() / "Desktop"


def find_executable() -> pathlib.Path:
    """The desktop app's launcher. Stable across updates (Squirrel stub)."""
    exe = APP_ROOT / "claude.exe"
    if not exe.exists():
        raise SystemExit(f"Claude-Desktop-App nicht gefunden unter {exe}")
    return exe


def profile_dir(name: str) -> pathlib.Path:
    """Sibling of the default profile, so list_profiles() discovers it."""
    default = ds.get_desktop_data_dir()
    return default.parent / f"{default.name}-{name}"


def profile_label(path: pathlib.Path) -> str:
    default = ds.get_desktop_data_dir()
    return "default" if path == default else path.name[len(default.name) + 1:]


def _account_of(path: pathlib.Path) -> tuple[str | None, ds.TokenCacheEntry | None]:
    """``(accountUuid, entry)`` for a profile, or ``(None, None)`` when signed out."""
    try:
        entries = ds.read_entries(path)
    except ds.DesktopStoreError as e:
        print(f"  ({path.name}: nicht lesbar — {e})", file=sys.stderr)
        return None, None
    if not entries:
        return None, None
    entry = entries.get("oauth:tokenCacheV2") or next(iter(entries.values()))
    return ds.read_active_account_uuid(path), entry


def cmd_list() -> int:
    profiles = ds.list_profiles()
    if not profiles:
        print("Keine Profile gefunden.")
        return 0
    print(f"{'Profil':<14} {'Plan':<26} {'Organisation':<38} Status")
    print("-" * 96)
    for path in profiles:
        _, entry = _account_of(path)
        if entry is None:
            print(f"{profile_label(path):<14} {'-':<26} {'-':<38} nicht angemeldet")
            continue
        plan = f"{entry.subscription_type} / {entry.rate_limit_tier}"
        print(f"{profile_label(path):<14} {plan:<26} {entry.organization_uuid:<38} angemeldet")
    return 0


# Files a new profile should inherit from the default one. Claude Code's own
# state (settings, projects, plans, skills) needs no copying -- it lives in
# ~/.claude, keyed by the home directory, so every profile already shares it.
# These two are Electron-profile-scoped and would otherwise start empty.
INHERITED_FILES = (
    "claude_desktop_config.json",  # MCP servers
    "config.json",  # UI preferences; the login inside it is not copied (see below)
)

# Keys of config.json that belong to the *account*, never to preferences. A new
# profile must start signed out -- copying these would seed it with the default
# profile's login, which is exactly the credential-swap the app rejects anyway.
ACCOUNT_SCOPED_KEYS = (*ds.TOKEN_CACHE_KEYS, ds.LAST_ACCOUNT_KEY)


def _inherit_from_default(path: pathlib.Path) -> list[str]:
    """Seed a new profile with the default's preferences, minus its login."""
    default = ds.get_desktop_data_dir()
    copied = []
    for name in INHERITED_FILES:
        src = default / name
        if not src.exists():
            continue
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if name == "config.json":
            data = {k: v for k, v in data.items() if k not in ACCOUNT_SCOPED_KEYS}
            # dxt:* keys are keyed by the default profile's organization and
            # mean nothing in another account's profile.
            data = {k: v for k, v in data.items() if not k.startswith("dxt:")}
        (path / name).write_text(json.dumps(data, indent=2), encoding="utf-8")
        copied.append(name)
    return copied


def cmd_create(name: str) -> int:
    path = profile_dir(name)
    if path.exists():
        print(f"Profil '{name}' existiert bereits: {path}")
        return 0
    path.mkdir(parents=True)
    copied = _inherit_from_default(path)
    print(f"Profil '{name}' angelegt: {path}")
    if copied:
        print(f"  Uebernommen aus dem Standardprofil: {', '.join(copied)}")
        print("  (ohne Login — dort meldest du dich gleich selbst an)")
    print("\nClaude Codes eigener Zustand (Einstellungen, Projekte, Skills) liegt in")
    print("~/.claude und wird von allen Profilen geteilt — nichts zu kopieren.")
    print(f"\nJetzt starten und mit dem gewuenschten Konto anmelden:")
    print(f"  python scripts/desktop_profiles.py launch {name}")
    return 0


def cmd_launch(name: str) -> int:
    path = profile_dir(name) if name != "default" else ds.get_desktop_data_dir()
    exe = find_executable()
    subprocess.Popen(
        [str(exe), f"--user-data-dir={path}"],
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        close_fds=True,
    )
    print(f"Gestartet: Profil '{name}' ({path})")
    return 0


def cmd_usage() -> int:
    """Live quota per profile, straight from the Anthropic usage API."""
    profiles = ds.list_profiles()
    if not profiles:
        print("Keine Profile gefunden.")
        return 0
    for path in profiles:
        label = profile_label(path)
        _, entry = _account_of(path)
        if entry is None:
            print(f"\n=== {label} ===\n  nicht angemeldet")
            continue
        print(f"\n=== {label} ===")
        print(f"  Plan : {entry.subscription_type} / {entry.rate_limit_tier}")
        try:
            data = oauth.request_usage_data(entry.token)
        except Exception as e:
            # An expired access token is normal for an idle profile; the app
            # refreshes it on next use. Report rather than guess a number.
            print(f"  Nutzung nicht abrufbar: {type(e).__name__} — {e}")
            continue
        _print_usage(data)
    return 0


def _print_usage(data: dict) -> None:
    """Render the usage payload's windows, tolerating shape changes."""
    windows = []
    for key, label in (("five_hour", "5 Stunden"), ("seven_day", "7 Tage")):
        w = data.get(key)
        if isinstance(w, dict):
            windows.append((label, w))
    for key, value in data.items():
        if key.startswith("seven_day_") and isinstance(value, dict):
            windows.append((key.replace("seven_day_", "7 Tage / "), value))
    if not windows:
        print(f"  (unbekanntes Antwortformat: {sorted(data)[:6]})")
        return
    for label, w in windows:
        pct = w.get("utilization")
        resets = w.get("resets_at")
        bar = ""
        if isinstance(pct, (int, float)):
            filled = int(round(pct / 5))
            bar = "[" + "#" * filled + "." * (20 - filled) + "]"
        print(f"  {label:<16} {bar} {pct if pct is not None else '?'}%   reset: {resets or '?'}")


def cmd_shortcuts() -> int:
    """Write one desktop shortcut per profile, so switching is a double-click."""
    exe = find_executable()
    profiles = ds.list_profiles()
    made = []
    for path in profiles:
        label = profile_label(path)
        lnk = DESKTOP / f"Claude ({label}).lnk"
        ps = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
            "$s.TargetPath = '%s';"
            "$s.Arguments = '--user-data-dir=\"%s\"';"
            "$s.IconLocation = '%s';"
            "$s.Save()"
        ) % (lnk, exe, path, APP_ROOT / "app.ico")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
        )
        if result.returncode == 0:
            made.append(lnk)
        else:
            print(f"  Verknuepfung fuer '{label}' fehlgeschlagen (rc={result.returncode})")
    for lnk in made:
        print(f"Verknuepfung angelegt: {lnk}")
    return 0 if made else 1


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command, *rest = argv
    if command == "list":
        return cmd_list()
    if command == "usage":
        return cmd_usage()
    if command == "shortcuts":
        return cmd_shortcuts()
    if command in ("create", "launch"):
        if not rest:
            print(f"Bitte einen Profilnamen angeben, z. B.: {command} privat")
            return 2
        return cmd_create(rest[0]) if command == "create" else cmd_launch(rest[0])
    print(f"Unbekanntes Kommando: {command}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ds.DesktopStoreError as exc:
        print(f"Fehler: {exc}")
        sys.exit(1)
