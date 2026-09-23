"""Static contract tests for the Windows daily-review scheduler scripts.

PowerShell is not available in CI, so these tests pin the structural contract
that mirrors the cron/launchd design in scripts/scheduler_install.py: the
installer persists credentials to a private per-user JSON file restricted to
the current user, the Scheduled Task carries only that file's path (never a
secret), and the trigger reads the file structurally at run time.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
INSTALLER = SCRIPTS / "install-daily-review.ps1"
TRIGGER = SCRIPTS / "daily-review-run-due.ps1"
DOC = Path(__file__).resolve().parents[1] / "docs" / "scheduled-daily-review.md"

SECRET_KEYS = ("LAB_TRACKER_API_KEY", "LAB_TRACKER_ADMIN_USER", "LAB_TRACKER_ADMIN_PASS")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _task_argument(installer: str) -> str:
    match = re.search(r"New-ScheduledTaskAction[^\n]*`\n\s*-Argument (\"[^\n]*\")", installer)
    assert match, "installer must build the task action with -Argument"
    return match.group(1)


def _base_url_pattern(installer: str) -> re.Pattern[str]:
    match = re.search(r"\$SafeBaseUrlPattern = '([^']+)'", installer)
    assert match, "installer must validate -BaseUrl against $SafeBaseUrlPattern"
    # .NET's \z (absolute end) is Python's \Z.
    return re.compile(match.group(1).replace(r"\z", r"\Z"))


def test_task_definition_carries_the_secrets_file_path_but_no_secret() -> None:
    installer = _text(INSTALLER)
    argument = _task_argument(installer)

    assert "-SecretsFile `\"$SecretsFile`\"" in argument
    assert "-LogFile `\"$LogFile`\"" in argument
    for key in SECRET_KEYS:
        assert key not in argument
    for secret_param in ("-ApiKey", "-AdminUser", "-AdminPass"):
        assert secret_param not in argument


def test_installer_persists_credentials_to_a_private_per_user_json_file() -> None:
    installer = _text(INSTALLER)

    assert "$env:LOCALAPPDATA" in installer
    assert "daily-review.secrets.json" in installer
    for key in SECRET_KEYS:
        assert f"$env:{key}" in installer
    assert "ConvertTo-Json" in installer
    # ACL: inheritance disabled and inherited rules dropped, current user only.
    assert "SetAccessRuleProtection($true, $false)" in installer
    assert "[System.Security.Principal.WindowsIdentity]::GetCurrent().User" in installer
    assert "FileSystemAccessRule" in installer
    assert "Set-Acl" in installer
    # The ACL is applied before any secret is written into the file.
    assert installer.index("Set-PrivateAcl -Path $SecretsFile") < installer.index(
        "WriteAllText($SecretsFile"
    )
    # A planted reparse point (symlink/junction) at the secrets path is refused.
    assert "ReparsePoint" in installer


def test_installer_keeps_existing_credentials_when_none_exported() -> None:
    installer = _text(INSTALLER)

    assert "keeping the existing credentials" in installer


def test_installer_completes_a_lone_admin_credential_from_the_stored_file() -> None:
    # Only LAB_TRACKER_ADMIN_PASS (or only _USER) set in the session must not
    # overwrite the file with half a login: the missing half is carried over
    # from the stored file, or the installer refuses before writing anything.
    installer = _text(INSTALLER)

    assert (
        '$credentials.Contains("LAB_TRACKER_ADMIN_USER") -xor '
        '$credentials.Contains("LAB_TRACKER_ADMIN_PASS")'
    ) in installer
    completion = installer.index("-xor")
    stored_read = installer.index("Get-Content -LiteralPath $SecretsFile -Raw", completion)
    carried = installer.index("$credentials[$missing] = [string]$stored", completion)
    refusal = installer.index("throw", completion)
    write = installer.index("WriteAllText($SecretsFile")
    assert completion < stored_read < write
    assert completion < carried < write
    assert completion < refusal < write
    # The refusal names the way out: drop the lone variable or add its pair.
    assert "unset $provided, or set $missing too" in installer
    assert "export both" not in installer


def test_installer_drops_a_stray_admin_half_next_to_an_api_key() -> None:
    # LAB_TRACKER_API_KEY is a complete credential that takes precedence at run
    # time, so a lone admin half beside it (with no stored pair to complete)
    # is dropped with a notice instead of refusing the install.
    installer = _text(INSTALLER)

    completion = installer.index("-xor")
    carried = installer.index("$credentials[$missing] = [string]$stored", completion)
    api_key_branch = installer.index(
        'elseif ($credentials.Contains("LAB_TRACKER_API_KEY"))', completion
    )
    dropped = installer.index("$credentials.Remove($provided)", completion)
    refusal = installer.index("throw", completion)
    write = installer.index("WriteAllText($SecretsFile")
    assert completion < carried < api_key_branch < dropped < refusal < write
    assert "LAB_TRACKER_API_KEY takes precedence" in installer
    doc = " ".join(_text(DOC).split())
    assert "takes precedence over the admin login at run time" in doc
    assert "unset the lone variable, or export its pair too" in doc


def test_installer_warns_for_a_remote_url_without_credentials() -> None:
    installer = _text(INSTALLER)

    assert "Write-Warning" in installer
    assert "no credential" in installer.lower()


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8000", "https://lab.example.org", "https://lab.example.org/api"]
)
def test_installer_base_url_pattern_accepts_plain_urls(url: str) -> None:
    assert _base_url_pattern(_text(INSTALLER)).match(url)


@pytest.mark.parametrize(
    "url",
    [
        'http://a" -ApiKey "x',
        "http://host`$(id)",
        "http://host;calc",
        "ftp://host",
        "http://host\n",
        "",
    ],
)
def test_installer_base_url_pattern_rejects_task_argument_breakouts(url: str) -> None:
    installer = _text(INSTALLER)
    assert "-cnotmatch $SafeBaseUrlPattern" in installer
    assert not _base_url_pattern(installer).match(url)


def test_trigger_reads_the_secrets_file_structurally() -> None:
    trigger = _text(TRIGGER)

    assert "[string]$SecretsFile = $env:LAB_TRACKER_SECRETS_FILE" in trigger
    assert "ConvertFrom-Json" in trigger
    assert "Get-Content -LiteralPath $SecretsFile -Raw" in trigger
    for key in SECRET_KEYS:
        assert f"$secrets.{key}" in trigger
    lowered = trigger.lower()
    assert "invoke-expression" not in lowered
    assert "iex " not in lowered
    assert ". $secretsfile" not in lowered


def test_trigger_records_failures_in_a_log_and_exits_non_zero() -> None:
    trigger = _text(TRIGGER)

    assert "[string]$LogFile" in trigger
    assert "Add-Content -LiteralPath $LogFile" in trigger
    assert "exit 1" in trigger


def test_docs_describe_the_windows_credential_file() -> None:
    doc = _text(DOC)

    assert "%LOCALAPPDATA%\\LabTracker\\daily-review.secrets.json" in doc
    assert "%LOCALAPPDATA%\\LabTracker\\daily-review.log" in doc
