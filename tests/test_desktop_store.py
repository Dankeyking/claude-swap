"""Tests for the Claude desktop app (Electron) credential store.

The OSCrypt primitives split cleanly: DPAPI key *unwrapping* is Windows-only and
gets patched out here, while the AES-256-GCM layer and every shape conversion are
platform-independent and run everywhere. That keeps the round-trip — the property
a switch depends on — under test on any CI runner.
"""

from __future__ import annotations

import base64
import json
import sys

import pytest

from claude_swap import desktop_store as ds

pytest.importorskip("cryptography", reason="desktop store needs AES-GCM")

KEY = b"\x11" * 32
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ORG_A = "23548bc0-f344-4e95-a649-bbecca7e0af6"
ORG_B = "77770000-1111-2222-3333-444455556666"
SCOPES = ["user:inference", "user:file_upload", "user:profile"]


def make_payload(org=ORG_A, token="tok-a", refresh="ref-a", scopes=SCOPES):
    key = f"{CLIENT_ID}:{org}:https://api.anthropic.com:{' '.join(scopes)}"
    return json.dumps(
        {
            key: {
                "token": token,
                "refreshToken": refresh,
                "expiresAt": 1816680908624,
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max",
            }
        }
    ).encode("utf-8")


# -- cache key parsing ----------------------------------------------------


def test_parse_cache_key_keeps_url_and_scopes_apart():
    client, org, url, scopes = ds._parse_cache_key(
        f"{CLIENT_ID}:{ORG_A}:https://api.anthropic.com:user:inference user:profile"
    )
    assert client == CLIENT_ID
    assert org == ORG_A
    # The URL's own colon must not be mistaken for a field separator, and the
    # scopes' colons must not be swallowed into the URL.
    assert url == "https://api.anthropic.com"
    assert scopes == ["user:inference", "user:profile"]


def test_parse_cache_key_rejects_malformed():
    with pytest.raises(ds.DesktopStoreError):
        ds._parse_cache_key("only:two:fields")


def test_cache_key_round_trips_verbatim():
    entry = ds.parse_token_cache(make_payload())
    original = json.loads(make_payload())
    assert entry.cache_key == next(iter(original))


# -- payload parsing ------------------------------------------------------


def test_parse_token_cache_reads_all_fields():
    entry = ds.parse_token_cache(make_payload())
    assert entry.token == "tok-a"
    assert entry.refresh_token == "ref-a"
    assert entry.expires_at == 1816680908624
    assert entry.subscription_type == "max"
    assert entry.rate_limit_tier == "default_claude_max"
    assert entry.organization_uuid == ORG_A


def test_parse_token_cache_rejects_multi_entry():
    """Two entries means we'd be guessing which account is active."""
    two = {
        f"{CLIENT_ID}:{ORG_A}:https://api.anthropic.com:user:profile": {
            "token": "a", "refreshToken": "ra", "expiresAt": 1,
        },
        f"{CLIENT_ID}:{ORG_B}:https://api.anthropic.com:user:profile": {
            "token": "b", "refreshToken": "rb", "expiresAt": 2,
        },
    }
    with pytest.raises(ds.DesktopStoreError, match="exactly one"):
        ds.parse_token_cache(json.dumps(two).encode())


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda v: v.pop("token"), id="no-token"),
        pytest.param(lambda v: v.pop("refreshToken"), id="no-refresh"),
        pytest.param(lambda v: v.update(expiresAt="soon"), id="non-int-expiry"),
    ],
)
def test_parse_token_cache_rejects_incomplete_entry(mutate):
    data = json.loads(make_payload())
    mutate(next(iter(data.values())))
    with pytest.raises(ds.DesktopStoreError):
        ds.parse_token_cache(json.dumps(data).encode())


def test_serialize_round_trips_through_parse():
    entry = ds.parse_token_cache(make_payload())
    assert ds.parse_token_cache(ds.serialize_token_cache(entry)) == entry


# -- credential shape conversion ------------------------------------------


def test_entry_to_credentials_speaks_claude_code_shape():
    creds = json.loads(ds.entry_to_credentials(ds.parse_token_cache(make_payload())))
    oauth = creds["claudeAiOauth"]
    assert oauth["accessToken"] == "tok-a"
    assert oauth["refreshToken"] == "ref-a"
    assert oauth["scopes"] == SCOPES
    assert oauth["subscriptionType"] == "max"
    # The org UUID has no home in Claude Code's shape but is needed to rebuild
    # the cache key, so it rides in the sidecar.
    assert creds["cswapDesktop"]["organizationUuid"] == ORG_A


def test_credentials_round_trip_preserves_entry():
    entry = ds.parse_token_cache(make_payload())
    assert ds.credentials_to_entry(ds.entry_to_credentials(entry)) == entry


def test_credentials_to_entry_rejects_cli_captured_login():
    """A CLI credential has no org UUID, so it cannot address the desktop cache."""
    cli_only = json.dumps(
        {"claudeAiOauth": {"accessToken": "t", "refreshToken": "r", "expiresAt": 1}}
    )
    with pytest.raises(ds.DesktopStoreError, match="organization UUID"):
        ds.credentials_to_entry(cli_only)


# -- OSCrypt layer --------------------------------------------------------


def test_encrypt_decrypt_round_trip():
    assert ds.decrypt_value(KEY, ds.encrypt_value(KEY, b"hello")) == b"hello"


def test_encrypt_emits_v10_envelope():
    blob = base64.b64decode(ds.encrypt_value(KEY, b"x"))
    assert blob[:3] == b"v10"
    # v10 + 12-byte nonce + ciphertext + 16-byte GCM tag
    assert len(blob) == 3 + 12 + 1 + 16


def test_encrypt_uses_a_fresh_nonce_each_time():
    assert ds.encrypt_value(KEY, b"same") != ds.encrypt_value(KEY, b"same")


def test_decrypt_rejects_foreign_prefix():
    blob = base64.b64encode(b"v20" + b"\x00" * 28).decode()
    with pytest.raises(ds.DesktopStoreError, match="prefix"):
        ds.decrypt_value(KEY, blob)


def test_decrypt_rejects_wrong_key():
    sealed = ds.encrypt_value(KEY, b"secret")
    with pytest.raises(ds.DesktopStoreError, match="decrypt"):
        ds.decrypt_value(b"\x22" * 32, sealed)


# -- store read/write against a fake data dir -----------------------------


@pytest.fixture
def fake_store(tmp_path, monkeypatch):
    """A config.json + patched master key, standing in for the real app dir."""
    monkeypatch.setattr(ds, "get_desktop_data_dir", lambda: tmp_path)
    monkeypatch.setattr(ds, "get_master_key", lambda: KEY)
    monkeypatch.setattr(ds, "desktop_app_running", lambda: False)
    config = {
        "locale": "de-DE",
        "oauth:tokenCache": ds.encrypt_value(KEY, make_payload()),
        "oauth:tokenCacheV2": ds.encrypt_value(KEY, make_payload()),
        ds.LAST_ACCOUNT_KEY: "04e1ec87-474c-4adb-92b7-804da6104bc3",
    }
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return tmp_path


def test_read_entries_decrypts_both_caches(fake_store):
    entries = ds.read_entries()
    assert set(entries) == set(ds.TOKEN_CACHE_KEYS)
    assert entries["oauth:tokenCacheV2"].token == "tok-a"


def test_read_active_credentials_prefers_v2(fake_store, monkeypatch):
    config = json.loads((fake_store / "config.json").read_text())
    config["oauth:tokenCacheV2"] = ds.encrypt_value(KEY, make_payload(token="tok-v2"))
    (fake_store / "config.json").write_text(json.dumps(config), encoding="utf-8")
    creds = json.loads(ds.read_active_credentials())
    assert creds["claudeAiOauth"]["accessToken"] == "tok-v2"


def test_read_active_credentials_empty_without_login(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "get_desktop_data_dir", lambda: tmp_path)
    monkeypatch.setattr(ds, "get_master_key", lambda: KEY)
    (tmp_path / "config.json").write_text(json.dumps({"locale": "de"}), encoding="utf-8")
    assert ds.read_active_credentials() == ""


def test_write_switches_both_caches_and_account_uuid(fake_store):
    target = ds.entry_to_credentials(
        ds.parse_token_cache(make_payload(org=ORG_B, token="tok-b", refresh="ref-b"))
    )
    ds.write_active_credentials(target, account_uuid="new-account-uuid")

    entries = ds.read_entries()
    for name in ds.TOKEN_CACHE_KEYS:
        assert entries[name].token == "tok-b"
        # The key must carry the *target's* org, or the app authenticates
        # against the wrong organization.
        assert entries[name].organization_uuid == ORG_B
    assert ds.read_active_account_uuid() == "new-account-uuid"


def test_write_preserves_unrelated_config_keys(fake_store):
    target = ds.entry_to_credentials(ds.parse_token_cache(make_payload(org=ORG_B)))
    ds.write_active_credentials(target, account_uuid="u")
    config = json.loads((fake_store / "config.json").read_text())
    assert config["locale"] == "de-DE"


def test_write_backs_up_the_previous_config(fake_store):
    before = (fake_store / "config.json").read_text()
    target = ds.entry_to_credentials(ds.parse_token_cache(make_payload(org=ORG_B)))
    ds.write_active_credentials(target, account_uuid="u")
    assert (fake_store / "config.json.cswap-bak").read_text() == before


def test_write_refuses_while_the_app_is_running(fake_store, monkeypatch):
    monkeypatch.setattr(ds, "desktop_app_running", lambda: True)
    target = ds.entry_to_credentials(ds.parse_token_cache(make_payload(org=ORG_B)))
    with pytest.raises(ds.DesktopAppRunning):
        ds.write_active_credentials(target, account_uuid="u")
    # Nothing was touched.
    assert ds.read_entries()["oauth:tokenCacheV2"].organization_uuid == ORG_A


def test_write_seeds_v2_when_no_cache_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "get_desktop_data_dir", lambda: tmp_path)
    monkeypatch.setattr(ds, "get_master_key", lambda: KEY)
    monkeypatch.setattr(ds, "desktop_app_running", lambda: False)
    (tmp_path / "config.json").write_text(json.dumps({"locale": "de"}), encoding="utf-8")
    target = ds.entry_to_credentials(ds.parse_token_cache(make_payload(org=ORG_B)))

    ds.write_active_credentials(target, account_uuid="u")

    entries = ds.read_entries()
    assert set(entries) == {"oauth:tokenCacheV2"}
    assert entries["oauth:tokenCacheV2"].organization_uuid == ORG_B


def test_write_leaves_account_uuid_alone_when_not_given(fake_store):
    target = ds.entry_to_credentials(ds.parse_token_cache(make_payload(org=ORG_B)))
    ds.write_active_credentials(target)
    assert ds.read_active_account_uuid() == "04e1ec87-474c-4adb-92b7-804da6104bc3"


def test_write_is_atomic_leaving_no_temp_files(fake_store):
    target = ds.entry_to_credentials(ds.parse_token_cache(make_payload(org=ORG_B)))
    ds.write_active_credentials(target, account_uuid="u")
    assert not list(fake_store.glob("*.tmp"))


# -- DPAPI wrapping for at-rest storage outside the app -------------------
#
# Storing a login as the *app's* ciphertext is not durable: a build migration
# re-encrypted the store under a different key on 2026-07-27 and stranded
# everything held that way. cswap therefore keeps the decrypted credential
# wrapped with the user's own DPAPI key, independent of the app's rotations.

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="DPAPI is a Windows API"
)


@windows_only
def test_protect_credentials_round_trips():
    creds = ds.entry_to_credentials(ds.parse_token_cache(make_payload()))
    assert ds.unprotect_credentials(ds.protect_credentials(creds)) == creds


@windows_only
def test_protect_credentials_does_not_store_plaintext():
    """The wrapped blob must not carry the token in the clear."""
    creds = ds.entry_to_credentials(ds.parse_token_cache(make_payload(token="tok-secret")))
    assert b"tok-secret" not in ds.protect_credentials(creds)


@windows_only
def test_protected_blob_survives_an_app_rekey():
    """DPAPI wrapping is independent of the app's OSCrypt key by construction."""
    creds = ds.entry_to_credentials(ds.parse_token_cache(make_payload()))
    blob = ds.protect_credentials(creds)
    # Whatever the app does to its own key, ours is untouched.
    assert ds.unprotect_credentials(blob) == creds


@windows_only
def test_unprotect_rejects_garbage():
    with pytest.raises(ds.DesktopStoreError):
        ds.unprotect_credentials(b"not a dpapi blob")


# -- running-app probe ----------------------------------------------------
#
# The probe shells out to tasklist, which writes in the console's OEM code page
# — not the ANSI one Python decodes with under text=True. Decoding there raised
# inside subprocess's reader thread and left stdout as None, which the probe then
# dereferenced. It now matches raw bytes and treats every inconclusive outcome as
# "not running".


@pytest.fixture
def on_windows(monkeypatch):
    from claude_swap.models import Platform

    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.WINDOWS))


def fake_tasklist(monkeypatch, *, stdout, returncode=0, raises=None):
    import subprocess

    def run(*args, **kwargs):
        if raises is not None:
            raise raises
        assert "text" not in kwargs, "probe must not decode; tasklist is OEM-encoded"
        return subprocess.CompletedProcess(args, returncode, stdout, b"")

    monkeypatch.setattr(subprocess, "run", run)


def test_probe_detects_a_running_app(on_windows, monkeypatch):
    fake_tasklist(monkeypatch, stdout=b"claude.exe   1234 Console   1   350.000 K\r\n")
    assert ds.desktop_app_running() is True


def test_probe_survives_oem_encoded_output(on_windows, monkeypatch):
    """0x81 is undecodable as cp1252 — the regression that crashed the probe."""
    fake_tasklist(
        monkeypatch,
        stdout=b"claude.exe  1234 Konsole\x81  1  350.000 K\r\n",
    )
    assert ds.desktop_app_running() is True


def test_probe_reports_not_running_on_empty_match(on_windows, monkeypatch):
    fake_tasklist(monkeypatch, stdout=b"INFO: No tasks are running.\r\n")
    assert ds.desktop_app_running() is False


def test_probe_fails_safe_on_none_stdout(on_windows, monkeypatch):
    """A reader-thread failure yields None; that must not raise."""
    fake_tasklist(monkeypatch, stdout=None)
    assert ds.desktop_app_running() is False


def test_probe_fails_safe_on_nonzero_exit(on_windows, monkeypatch):
    fake_tasklist(monkeypatch, stdout=b"claude.exe", returncode=1)
    assert ds.desktop_app_running() is False


def test_probe_fails_safe_when_tasklist_is_missing(on_windows, monkeypatch):
    fake_tasklist(monkeypatch, stdout=None, raises=OSError("not found"))
    assert ds.desktop_app_running() is False


def test_probe_is_false_off_windows(monkeypatch):
    from claude_swap.models import Platform

    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.LINUX))
    assert ds.desktop_app_running() is False


# -- platform guards ------------------------------------------------------


def test_master_key_unsupported_off_windows(monkeypatch):
    from claude_swap.models import Platform

    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.LINUX))
    with pytest.raises(ds.DesktopStoreUnsupported):
        ds.get_master_key()


def test_data_dir_unsupported_on_linux(monkeypatch):
    from claude_swap.models import Platform

    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.LINUX))
    with pytest.raises(ds.DesktopStoreUnsupported):
        ds.get_desktop_data_dir()


def test_store_present_is_non_raising_off_windows(monkeypatch):
    from claude_swap.models import Platform

    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.LINUX))
    assert ds.desktop_store_present() is False


def test_data_dir_follows_appdata(monkeypatch, tmp_path):
    from claude_swap.models import Platform

    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.WINDOWS))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert ds.get_desktop_data_dir() == tmp_path / "Claude"
    assert ds.get_desktop_config_path() == tmp_path / "Claude" / "config.json"
    assert ds.get_desktop_local_state_path() == tmp_path / "Claude" / "Local State"


def test_master_key_rejects_short_key(monkeypatch, tmp_path):
    """A DPAPI unwrap that yields the wrong length must not be used as an AES key."""
    from claude_swap.models import Platform

    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.WINDOWS))
    monkeypatch.setattr(ds, "get_desktop_data_dir", lambda: tmp_path)
    monkeypatch.setattr(ds, "_dpapi_unprotect", lambda blob: b"short")
    (tmp_path / "Local State").write_text(
        json.dumps({"os_crypt": {"encrypted_key": base64.b64encode(b"DPAPIxxxx").decode()}}),
        encoding="utf-8",
    )
    with pytest.raises(ds.DesktopStoreError, match="32"):
        ds.get_master_key()


def test_master_key_rejects_foreign_wrapper(monkeypatch, tmp_path):
    from claude_swap.models import Platform

    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.WINDOWS))
    monkeypatch.setattr(ds, "get_desktop_data_dir", lambda: tmp_path)
    (tmp_path / "Local State").write_text(
        json.dumps({"os_crypt": {"encrypted_key": base64.b64encode(b"OTHERxxxx").decode()}}),
        encoding="utf-8",
    )
    with pytest.raises(ds.DesktopStoreError, match="wrapper"):
        ds.get_master_key()
