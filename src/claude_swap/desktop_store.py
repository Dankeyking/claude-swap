"""Credential storage for the Claude *desktop app* (Electron).

The desktop app bundles stock Claude Code but does **not** use Claude Code's own
credential locations: no ``~/.claude/.credentials.json``, no macOS
``Claude Code-credentials`` Keychain item. Its OAuth login lives in the Electron
shell's own store, encrypted with Chromium's OSCrypt scheme:

    Local State  → os_crypt.encrypted_key → (strip "DPAPI" prefix) → DPAPI → 32-byte AES key
    config.json  → "oauth:tokenCache" / "oauth:tokenCacheV2"
                 → base64 → b"v10" + 12-byte nonce + AES-256-GCM ciphertext → JSON

The decrypted payload is a single-entry object keyed by a colon-joined tuple::

    "<client_id>:<organization_uuid>:<base_url>:<space separated scopes>": {
        "token": …, "refreshToken": …, "expiresAt": …,
        "subscriptionType": …, "rateLimitTier": …
    }

Crucially the ``client_id`` is the *same* OAuth client Claude Code uses
(``oauth.OAUTH_CLIENT_ID``), so tokens read from here refresh and authenticate
against the profile/usage endpoints through the existing ``oauth`` module with no
changes. Only *storage* differs — which is why this module is a storage leaf: it
converts between the desktop shape and the ``{"claudeAiOauth": …}`` shape the rest
of cswap already speaks, and does nothing else.

Two behaviours the callers must respect:

* **The app must not be running during a write.** Electron holds its store in
  memory and flushes on exit, so writing under a live app is liable to be
  clobbered. :func:`desktop_app_running` reports this; the switch path refuses.
* **The app reads the store at startup.** A switch therefore takes effect on the
  next launch, not hot — unlike the CLI's file-mtime hot reload.

Only Windows is implemented. macOS stores the same ``v10`` payload but derives the
key from a Keychain secret ("Claude Safe Storage") via PBKDF2, and Linux uses
either a fixed "peanuts" password or the desktop keyring; both raise
:class:`DesktopStoreUnsupported` here rather than guess.

References:
- Chromium os_crypt: components/os_crypt/sync/os_crypt_win.cc
- Electron safeStorage: shell/browser/api/electron_api_safe_storage.cc
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import sys
from pathlib import Path
from typing import NamedTuple

from claude_swap.exceptions import CredentialError
from claude_swap.models import Platform

_logger = logging.getLogger("claude-swap")

# Keys the Electron store uses for the OAuth token cache. V2 carries the wider
# scope set (it adds ``user:sessions`` and ``claude_code``) and is what current
# builds read; V1 is kept in step so an older build — or a rollback — doesn't
# find one account's token beside another's.
TOKEN_CACHE_KEYS = ("oauth:tokenCache", "oauth:tokenCacheV2")

# The account UUID the app remembers across restarts. Left stale it makes the
# app open on the previous account's identity while holding the new token.
LAST_ACCOUNT_KEY = "lastKnownAccountUuid"

# Chromium's ciphertext version prefix and DPAPI key wrapper marker.
_V10_PREFIX = b"v10"
_NONCE_LEN = 12
_DPAPI_PREFIX = b"DPAPI"

# Process image name of the desktop app, for the is-it-running guard.
_DESKTOP_PROCESS_NAME = "claude.exe"


class DesktopStoreError(CredentialError):
    """The desktop credential store exists but could not be read or written."""


class DesktopStoreUnsupported(DesktopStoreError):
    """No desktop store on this platform, or its crypto is not implemented here."""


class DesktopAppRunning(DesktopStoreError):
    """Refused to write while the desktop app is running (it would clobber us)."""


class TokenCacheEntry(NamedTuple):
    """One decrypted desktop token-cache entry, key and value together.

    ``client_id``/``organization_uuid``/``base_url``/``scopes`` come from the
    colon-joined cache key; the rest from its object. Kept as one value because a
    round-trip has to rebuild the key verbatim — a switch writes the *target*
    account's organization UUID, and getting that wrong silently authenticates
    against the wrong org.
    """

    client_id: str
    organization_uuid: str
    base_url: str
    scopes: list[str]
    token: str
    refresh_token: str
    expires_at: int
    subscription_type: str | None
    rate_limit_tier: str | None

    @property
    def cache_key(self) -> str:
        """Rebuild the colon-joined store key this entry is filed under."""
        return ":".join(
            (
                self.client_id,
                self.organization_uuid,
                self.base_url,
                " ".join(self.scopes),
            )
        )


# -- platform paths -------------------------------------------------------


def get_desktop_data_dir() -> Path:
    """Return the desktop app's Electron user-data directory.

    Raises:
        DesktopStoreUnsupported: On platforms with no known location.
    """
    platform = Platform.detect()
    if platform == Platform.WINDOWS:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise DesktopStoreUnsupported("APPDATA is not set")
        return Path(appdata) / "Claude"
    if platform == Platform.MACOS:
        return Path.home() / "Library" / "Application Support" / "Claude"
    raise DesktopStoreUnsupported(
        f"No known Claude desktop app data directory for {platform.name.lower()}"
    )


def get_desktop_config_path() -> Path:
    """Path of the Electron settings file holding the encrypted token cache."""
    return get_desktop_data_dir() / "config.json"


def get_desktop_local_state_path() -> Path:
    """Path of the Chromium ``Local State`` file holding the wrapped master key."""
    return get_desktop_data_dir() / "Local State"


def desktop_store_present() -> bool:
    """Whether a readable desktop store exists on this machine (non-raising)."""
    try:
        return get_desktop_config_path().exists() and get_desktop_local_state_path().exists()
    except DesktopStoreUnsupported:
        return False


def desktop_app_running() -> bool:
    """Whether the desktop app currently has a process running.

    Best-effort and deliberately conservative in the *unknown* direction: an
    inconclusive probe reports ``False`` (not running) so a missing/blocked
    tasklist can't make switching permanently impossible. The write path pairs
    this with a post-write verification read, which catches a clobber the probe
    missed.
    """
    if Platform.detect() != Platform.WINDOWS:
        return False
    import subprocess

    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {_DESKTOP_PROCESS_NAME}", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as e:
        _logger.debug("Could not probe for a running desktop app: %s", e)
        return False
    return _DESKTOP_PROCESS_NAME.lower() in out.stdout.lower()


# -- OSCrypt primitives ---------------------------------------------------


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_unprotect(blob: bytes) -> bytes:
    """Unwrap a DPAPI blob with the current user's key (Windows only)."""
    if sys.platform != "win32":  # pragma: no cover - guarded by callers
        raise DesktopStoreUnsupported("DPAPI is Windows-only")
    buf = ctypes.create_string_buffer(blob, len(blob))
    blob_in = _DataBlob(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise DesktopStoreError(
            f"CryptUnprotectData failed (Windows error {ctypes.GetLastError()}). "
            "The store is bound to the Windows user that created it."
        )
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise DesktopStoreError(f"{path.name} not found at {path}") from e
    except (OSError, json.JSONDecodeError) as e:
        raise DesktopStoreError(f"Could not read {path}: {e}") from e
    if not isinstance(data, dict):
        raise DesktopStoreError(f"{path} is not a JSON object")
    return data


def get_master_key() -> bytes:
    """Return the 32-byte AES key that protects the desktop app's secrets.

    Raises:
        DesktopStoreError: If ``Local State`` is missing, malformed, or the DPAPI
            unwrap fails (typically: a different Windows user).
        DesktopStoreUnsupported: On non-Windows platforms.
    """
    if Platform.detect() != Platform.WINDOWS:
        raise DesktopStoreUnsupported(
            "Reading the Claude desktop store is only implemented on Windows"
        )
    state = _read_json(get_desktop_local_state_path())
    try:
        encoded = state["os_crypt"]["encrypted_key"]
    except (KeyError, TypeError) as e:
        raise DesktopStoreError("Local State has no os_crypt.encrypted_key") from e
    try:
        wrapped = base64.b64decode(encoded)
    except (ValueError, TypeError) as e:
        raise DesktopStoreError("os_crypt.encrypted_key is not valid base64") from e
    if not wrapped.startswith(_DPAPI_PREFIX):
        raise DesktopStoreError(
            f"Unexpected key wrapper {wrapped[:5]!r}; expected {_DPAPI_PREFIX!r}"
        )
    key = _dpapi_unprotect(wrapped[len(_DPAPI_PREFIX):])
    if len(key) != 32:
        raise DesktopStoreError(f"Master key is {len(key)} bytes, expected 32")
    return key


def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:  # pragma: no cover - dependency is declared
        raise DesktopStoreUnsupported(
            "The 'cryptography' package is required to read the desktop store"
        ) from e
    return AESGCM(key)


def decrypt_value(key: bytes, encoded: str) -> bytes:
    """Decrypt one base64 ``v10`` OSCrypt value."""
    try:
        blob = base64.b64decode(encoded)
    except (ValueError, TypeError) as e:
        raise DesktopStoreError("Encrypted value is not valid base64") from e
    if not blob.startswith(_V10_PREFIX):
        raise DesktopStoreError(
            f"Unexpected ciphertext prefix {blob[:3]!r}; expected {_V10_PREFIX!r}"
        )
    nonce = blob[len(_V10_PREFIX):len(_V10_PREFIX) + _NONCE_LEN]
    payload = blob[len(_V10_PREFIX) + _NONCE_LEN:]
    try:
        return _aesgcm(key).decrypt(nonce, payload, None)
    except Exception as e:
        raise DesktopStoreError(f"Could not decrypt the desktop token cache: {e}") from e


def encrypt_value(key: bytes, plaintext: bytes) -> str:
    """Encrypt to a base64 ``v10`` OSCrypt value the app will accept."""
    nonce = os.urandom(_NONCE_LEN)
    sealed = _aesgcm(key).encrypt(nonce, plaintext, None)
    return base64.b64encode(_V10_PREFIX + nonce + sealed).decode("ascii")


# -- token cache <-> claude-swap credential shape -------------------------


def _parse_cache_key(raw: str) -> tuple[str, str, str, list[str]]:
    """Split ``client:org:https://host:scope scope`` into its four parts.

    Split from the left on the first two colons only — the base URL contains
    colons of its own, and the scope list is whatever follows the URL.
    """
    client_id, _, rest = raw.partition(":")
    org_uuid, _, rest = rest.partition(":")
    scheme, sep, tail = rest.partition("://")
    if not sep:
        raise DesktopStoreError(f"Malformed token-cache key: {raw!r}")
    host, _, scopes = tail.partition(":")
    if not client_id or not org_uuid or not host:
        raise DesktopStoreError(f"Malformed token-cache key: {raw!r}")
    return client_id, org_uuid, f"{scheme}://{host}", scopes.split() if scopes else []


def parse_token_cache(plaintext: bytes) -> TokenCacheEntry:
    """Parse a decrypted token-cache payload into its single entry.

    Raises:
        DesktopStoreError: If the payload is not a one-entry object of the
            expected shape. Refusing a multi-entry payload is deliberate: the
            app has only ever written one, and silently picking the first would
            be a coin flip over which account is active.
    """
    try:
        data = json.loads(plaintext)
    except json.JSONDecodeError as e:
        raise DesktopStoreError(f"Token cache is not JSON: {e}") from e
    if not isinstance(data, dict) or len(data) != 1:
        raise DesktopStoreError(
            f"Expected exactly one token-cache entry, found {len(data) if isinstance(data, dict) else 'non-object'}"
        )
    raw_key, value = next(iter(data.items()))
    if not isinstance(value, dict):
        raise DesktopStoreError("Token-cache entry is not an object")
    client_id, org_uuid, base_url, scopes = _parse_cache_key(raw_key)
    token = value.get("token")
    refresh = value.get("refreshToken")
    if not isinstance(token, str) or not isinstance(refresh, str):
        raise DesktopStoreError("Token-cache entry is missing token/refreshToken")
    expires_at = value.get("expiresAt")
    if not isinstance(expires_at, int):
        raise DesktopStoreError("Token-cache entry has no integer expiresAt")
    return TokenCacheEntry(
        client_id=client_id,
        organization_uuid=org_uuid,
        base_url=base_url,
        scopes=scopes,
        token=token,
        refresh_token=refresh,
        expires_at=expires_at,
        subscription_type=value.get("subscriptionType"),
        rate_limit_tier=value.get("rateLimitTier"),
    )


def serialize_token_cache(entry: TokenCacheEntry) -> bytes:
    """Render an entry back into the payload shape the app reads."""
    value: dict = {
        "token": entry.token,
        "refreshToken": entry.refresh_token,
        "expiresAt": entry.expires_at,
    }
    if entry.subscription_type is not None:
        value["subscriptionType"] = entry.subscription_type
    if entry.rate_limit_tier is not None:
        value["rateLimitTier"] = entry.rate_limit_tier
    return json.dumps({entry.cache_key: value}).encode("utf-8")


def entry_to_credentials(entry: TokenCacheEntry) -> str:
    """Convert a desktop entry to the ``{"claudeAiOauth": …}`` string cswap uses.

    The organization UUID and rate-limit tier have no home in Claude Code's own
    credential shape but are needed verbatim to rebuild the cache key on the way
    back, so they ride along under a cswap-private key that the OAuth code
    ignores.
    """
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": entry.token,
                "refreshToken": entry.refresh_token,
                "expiresAt": entry.expires_at,
                "scopes": list(entry.scopes),
                "subscriptionType": entry.subscription_type,
            },
            "cswapDesktop": {
                "clientId": entry.client_id,
                "organizationUuid": entry.organization_uuid,
                "baseUrl": entry.base_url,
                "rateLimitTier": entry.rate_limit_tier,
            },
        }
    )


def credentials_to_entry(credentials: str) -> TokenCacheEntry:
    """Rebuild a desktop entry from a cswap credential string.

    Raises:
        DesktopStoreError: If the credential lacks an OAuth login or the desktop
            sidecar written by :func:`entry_to_credentials`. A credential captured
            from the *CLI* has no sidecar: it cannot be activated in the desktop
            app, because its organization UUID — part of the cache key the app
            looks itself up by — was never recorded.
    """
    try:
        data = json.loads(credentials)
    except (json.JSONDecodeError, TypeError) as e:
        raise DesktopStoreError(f"Credential is not JSON: {e}") from e
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    sidecar = data.get("cswapDesktop") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        raise DesktopStoreError("Credential carries no claudeAiOauth login")
    if not isinstance(sidecar, dict) or not sidecar.get("organizationUuid"):
        raise DesktopStoreError(
            "Credential has no desktop organization UUID — it was captured from "
            "the CLI and cannot be activated in the desktop app"
        )
    token = oauth.get("accessToken")
    refresh = oauth.get("refreshToken")
    if not isinstance(token, str) or not isinstance(refresh, str):
        raise DesktopStoreError("Credential is missing accessToken/refreshToken")
    scopes = oauth.get("scopes")
    return TokenCacheEntry(
        client_id=sidecar.get("clientId") or "",
        organization_uuid=sidecar["organizationUuid"],
        base_url=sidecar.get("baseUrl") or "https://api.anthropic.com",
        scopes=list(scopes) if isinstance(scopes, list) else [],
        token=token,
        refresh_token=refresh,
        expires_at=int(oauth.get("expiresAt") or 0),
        subscription_type=oauth.get("subscriptionType"),
        rate_limit_tier=sidecar.get("rateLimitTier"),
    )


# -- read / write ---------------------------------------------------------


def read_entries() -> dict[str, TokenCacheEntry]:
    """Decrypt every present token cache, keyed by its store key.

    Missing caches are omitted rather than erroring: a build that only writes V2
    is normal. An empty result means no desktop login exists.
    """
    key = get_master_key()
    config = _read_json(get_desktop_config_path())
    entries: dict[str, TokenCacheEntry] = {}
    for name in TOKEN_CACHE_KEYS:
        raw = config.get(name)
        if not isinstance(raw, str) or not raw:
            continue
        entries[name] = parse_token_cache(decrypt_value(key, raw))
    return entries


def read_active_credentials() -> str:
    """Read the desktop app's active login as a cswap credential string.

    Returns ``""`` when the app has no login stored. Prefers the V2 cache (the
    wider scope set current builds use) and falls back to V1.
    """
    entries = read_entries()
    for name in reversed(TOKEN_CACHE_KEYS):  # V2 first
        if name in entries:
            return entry_to_credentials(entries[name])
    return ""


def read_active_account_uuid() -> str | None:
    """Read ``lastKnownAccountUuid``, or None when the app has never signed in."""
    value = _read_json(get_desktop_config_path()).get(LAST_ACCOUNT_KEY)
    return value if isinstance(value, str) and value else None


def backup_config(suffix: str = ".cswap-bak") -> Path:
    """Copy ``config.json`` beside itself before a write. Returns the copy's path.

    One generation, overwritten each time: the point is recovering the *previous*
    login if a write goes wrong, and a login two switches ago is stale anyway.
    """
    import shutil

    path = get_desktop_config_path()
    backup = path.with_name(path.name + suffix)
    shutil.copy2(path, backup)
    return backup


def write_active_credentials(
    credentials: str, account_uuid: str | None = None, *, allow_running: bool = False
) -> None:
    """Activate a login in the desktop app's store.

    Rewrites both token caches with the target's material, updates
    ``lastKnownAccountUuid``, and leaves every other key in ``config.json``
    untouched. Takes effect the next time the app starts.

    Args:
        credentials: A cswap credential string carrying a desktop sidecar (see
            :func:`entry_to_credentials`).
        account_uuid: The target's ``oauthAccount.accountUuid``. Omitted leaves
            the stored value alone, which is wrong after a real switch — pass it.
        allow_running: Skip the running-app guard. For tests and forced recovery
            only; a live app flushes its in-memory store on exit and will
            overwrite whatever we wrote.

    Raises:
        DesktopAppRunning: If the desktop app is running and ``allow_running``
            is not set.
        DesktopStoreError: On a malformed credential or a failed write. The
            pre-write backup (``config.json.cswap-bak``) is left in place.
    """
    if not allow_running and desktop_app_running():
        raise DesktopAppRunning(
            "The Claude desktop app is running. Quit it before switching — a "
            "running app flushes its own store on exit and would overwrite the "
            "switch."
        )

    entry = credentials_to_entry(credentials)
    key = get_master_key()
    path = get_desktop_config_path()
    config = _read_json(path)

    backup = backup_config()
    _logger.debug("Backed up desktop config to %s", backup)

    payload = serialize_token_cache(entry)
    for name in TOKEN_CACHE_KEYS:
        if name in config:
            config[name] = encrypt_value(key, payload)
    if not any(name in config for name in TOKEN_CACHE_KEYS):
        # First-ever write (or a store that never held a login): seed V2, the
        # cache current builds read.
        config[TOKEN_CACHE_KEYS[-1]] = encrypt_value(key, payload)
    if account_uuid:
        config[LAST_ACCOUNT_KEY] = account_uuid

    _atomic_write_json(path, config)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically replace a JSON file, preserving unknown keys as given.

    Separate from ``settings.atomic_write_json`` on purpose: that one owns the
    cswap backup dir's 0600/0700 posture, which must not be imposed on a file
    belonging to another application.
    """
    import tempfile

    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data).encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(path))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
