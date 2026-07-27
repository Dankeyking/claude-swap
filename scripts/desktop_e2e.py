"""End-to-end harness for the desktop-app credential store.

Drives desktop_store.py against the *real* Claude desktop app so the switch can
be validated without an assistant session running inside the app being switched.
Run it from an ordinary terminal while the app is closed.

    uv run python scripts/desktop_e2e.py status
    uv run python scripts/desktop_e2e.py snapshot B
    uv run python scripts/desktop_e2e.py activate A
    uv run python scripts/desktop_e2e.py list

A snapshot stores the *decrypted* login, re-wrapped with the current user's
DPAPI key -- deliberately not a copy of the app's own ciphertext. An app build
migration re-encrypted the store under a different key on 2026-07-27, which
stranded every snapshot taken as ciphertext; storing the credential instead
survives that, and mirrors what the real integration does (decrypt on add,
re-encrypt with the then-current key on switch). Snapshots live outside the
repository, in ~/.claude-swap-backup/desktop-e2e/.

Token values are never printed; only sha256 fingerprints, so two runs can be
compared without revealing anything.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys

from claude_swap import desktop_store as ds

SNAP_DIR = pathlib.Path.home() / ".claude-swap-backup" / "desktop-e2e"
GLOBAL_CONFIG = pathlib.Path.home() / ".claude.json"


def fp(value: str) -> str:
    """Short non-reversible fingerprint for comparing without revealing."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _snapshot_paths(label: str) -> tuple[pathlib.Path, pathlib.Path]:
    return SNAP_DIR / f"cred.{label}.dpapi", SNAP_DIR / f"account.{label}.json"


def _describe(entry: ds.TokenCacheEntry, indent: str = "  ") -> None:
    print(f"{indent}organizationUuid : {entry.organization_uuid}")
    print(f"{indent}subscriptionType : {entry.subscription_type} / {entry.rate_limit_tier}")
    print(f"{indent}scopes           : {' '.join(entry.scopes)}")
    print(f"{indent}Access-Token  fp : {fp(entry.token)}")
    print(f"{indent}Refresh-Token fp : {fp(entry.refresh_token)}")


def cmd_status() -> int:
    """Show what the live store currently holds."""
    print(f"Datenordner : {ds.get_desktop_data_dir()}")
    print(f"App laeuft  : {ds.desktop_app_running()}")
    entries = ds.read_entries()
    if not entries:
        print("Kein Login im Store.")
        return 0
    for name, entry in entries.items():
        print(f"\n{name}")
        _describe(entry)
    print(f"\nlastKnownAccountUuid : {ds.read_active_account_uuid()}")
    acct = _read_oauth_account()
    print(f"~/.claude.json Konto : {acct.get('emailAddress')} ({acct.get('accountUuid')})")
    return 0


def _read_oauth_account() -> dict:
    try:
        data = json.loads(GLOBAL_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    acct = data.get("oauthAccount")
    return acct if isinstance(acct, dict) else {}


def cmd_snapshot(label: str) -> int:
    """Store the live login under ``label``, decrypted and DPAPI-re-wrapped."""
    credentials = ds.read_active_credentials()
    if not credentials:
        print(f"Kein Login im Store — Snapshot '{label}' nicht angelegt.")
        return 1

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    cred_dst, account_dst = _snapshot_paths(label)
    cred_dst.write_bytes(ds.protect_credentials(credentials))
    acct = _read_oauth_account()
    account_dst.write_text(json.dumps(acct, indent=2), encoding="utf-8")

    entry = ds.credentials_to_entry(credentials)
    print(f"Snapshot '{label}' gespeichert:")
    print(f"  E-Mail           : {acct.get('emailAddress')}")
    print(f"  accountUuid      : {acct.get('accountUuid')}")
    _describe(entry)

    # Prove it reads back before the user relies on it.
    roundtrip = ds.unprotect_credentials(cred_dst.read_bytes())
    print(f"  Rueckleseprobe   : {'OK' if roundtrip == credentials else 'FEHLGESCHLAGEN'}")
    return 0 if roundtrip == credentials else 1


def cmd_activate(label: str) -> int:
    """Write the snapshot's login into the live store via the real write path."""
    cred_src, account_src = _snapshot_paths(label)
    if not cred_src.exists():
        print(f"Kein Snapshot '{label}' unter {cred_src}")
        return 1

    if ds.desktop_app_running():
        print(
            "Die Claude-Desktop-App laeuft. Bitte vollstaendig beenden "
            "(auch aus dem Infobereich/Tray) und erneut versuchen."
        )
        return 1

    credentials = ds.unprotect_credentials(cred_src.read_bytes())
    if not credentials:
        print(f"Snapshot '{label}' enthaelt keinen Login.")
        return 1

    acct = {}
    if account_src.exists():
        try:
            acct = json.loads(account_src.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    ds.write_active_credentials(credentials, account_uuid=acct.get("accountUuid"))
    if acct:
        _restore_oauth_account(acct)

    print(f"Snapshot '{label}' aktiviert.")
    print(f"  E-Mail      : {acct.get('emailAddress')}")
    print(f"  accountUuid : {acct.get('accountUuid')}")
    print("\nKontrolle des geschriebenen Stores:")
    for name, entry in ds.read_entries().items():
        print(f"\n{name}")
        _describe(entry)
    print("\nJetzt die Desktop-App starten und pruefen, welches Konto aktiv ist.")
    return 0


def _restore_oauth_account(acct: dict) -> None:
    """Put the snapshot's oauthAccount back into ~/.claude.json, key-scoped.

    Only this one key is touched; projects, settings and everything else in the
    file pass through untouched.
    """
    import os
    import tempfile

    try:
        data = json.loads(GLOBAL_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  (oauthAccount nicht zurueckgeschrieben: {e})")
        return
    data["oauthAccount"] = acct
    fd, tmp = tempfile.mkstemp(dir=str(GLOBAL_CONFIG.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp, str(GLOBAL_CONFIG))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cmd_list() -> int:
    if not SNAP_DIR.exists():
        print("Noch keine Snapshots.")
        return 0
    labels = sorted(p.name[len("cred."):-len(".dpapi")] for p in SNAP_DIR.glob("cred.*.dpapi"))
    if not labels:
        print("Noch keine Snapshots.")
        return 0
    print(f"Snapshots in {SNAP_DIR}:")
    for label in labels:
        _, account_src = _snapshot_paths(label)
        email = "?"
        if account_src.exists():
            try:
                email = json.loads(account_src.read_text(encoding="utf-8")).get(
                    "emailAddress", "?"
                )
            except json.JSONDecodeError:
                pass
        print(f"  {label:<8} {email}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command, *rest = argv
    if command == "status":
        return cmd_status()
    if command == "list":
        return cmd_list()
    if command in ("snapshot", "activate"):
        if not rest:
            print(f"Bitte ein Label angeben, z. B.: {command} B")
            return 2
        return cmd_snapshot(rest[0]) if command == "snapshot" else cmd_activate(rest[0])
    print(f"Unbekanntes Kommando: {command}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ds.DesktopStoreError as exc:
        print(f"Fehler: {exc}")
        sys.exit(1)
