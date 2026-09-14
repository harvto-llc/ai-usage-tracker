import subprocess
from unittest.mock import MagicMock, patch

from src.credential_store import (
    KEYCHAIN_SERVICE,
    load_provider_credential,
    store_provider_credential,
)


@patch("src.credential_store.keychain_enabled", return_value=True)
@patch("src.credential_store.subprocess.run")
def test_store_passes_long_secret_on_stdin_not_command_line(mock_run, _mock_enabled):
    secret = "sessionKey=" + ("private-value-" * 20)
    mock_run.side_effect = [
        MagicMock(returncode=0),
        MagicMock(returncode=0, stdout=f"{secret}\n"),
    ]

    assert store_provider_credential("claude", secret) is True

    args, kwargs = mock_run.call_args_list[0]
    assert secret not in args[0]
    assert args[0][-1] == "-i"
    assert secret in kwargs["input"]
    assert kwargs["input"].startswith("add-generic-password -U")
    assert kwargs["capture_output"] is True
    assert mock_run.call_count == 2


@patch("src.credential_store.keychain_enabled", return_value=True)
@patch("src.credential_store.subprocess.run")
def test_load_returns_keychain_value(mock_run, _mock_enabled):
    mock_run.return_value = MagicMock(returncode=0, stdout="cookie=value\n")

    assert load_provider_credential("codex") == "cookie=value"
    command = mock_run.call_args.args[0]
    assert command[-1] == "-w"
    assert KEYCHAIN_SERVICE in command


@patch("src.credential_store.keychain_enabled", return_value=True)
@patch("src.credential_store.subprocess.run", side_effect=subprocess.TimeoutExpired("security", 5))
def test_keychain_failure_degrades_to_missing(mock_run, _mock_enabled):
    assert load_provider_credential("claude") is None
    assert store_provider_credential("claude", "secret") is False
    assert mock_run.call_count == 2


def test_unsupported_provider_is_not_stored():
    assert store_provider_credential("cursor", "secret") is False
    assert load_provider_credential("cursor") is None
