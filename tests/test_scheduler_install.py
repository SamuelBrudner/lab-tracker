"""Unit tests for the daily-review scheduler installer implementation.

Loaded via importlib (the module lives under scripts/ so cron/launchd can run it
with the system python3), mirroring test_analysis_graph_draft_script.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "scheduler_install.py"
CRON_INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install-daily-review.sh"

# Credentials engineered to break shell interpolation / sourcing or inject JSON.
ADVERSARIAL_SECRETS = [
    "pass word",
    'quote"inside',
    "single'quote",
    "$(rm -rf ~)",
    "`whoami`",
    "line\nbreak",
    "back\\slash",
    "semi;colon && echo pwned",
    "brace}{",
]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scheduler_install", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()


class TestValidateInterval:
    def test_accepts_positive_int(self):
        assert mod.validate_interval("15") == 15
        assert mod.validate_interval(30) == 30

    @pytest.mark.parametrize(
        "bad", ["0", "-5", "abc", "", "15.5", str(mod.MAX_INTERVAL_MINUTES + 1)]
    )
    def test_rejects_invalid(self, bad):
        with pytest.raises(mod.SchedulerConfigError):
            mod.validate_interval(bad)


class TestValidateBaseUrl:
    def test_accepts_http_and_https_and_strips_trailing_slash(self):
        assert mod.validate_base_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
        assert mod.validate_base_url("https://lab.example.org") == "https://lab.example.org"

    @pytest.mark.parametrize(
        "bad",
        ["", "ftp://host", "not a url", "javascript:alert(1)", "http://ho st", "//host"],
    )
    def test_rejects_invalid(self, bad):
        with pytest.raises(mod.SchedulerConfigError):
            mod.validate_base_url(bad)

    @pytest.mark.parametrize(
        "payload",
        [
            'http://a";touch${IFS}/tmp/proof;"',  # quote/`;` breakout, IFS dodges whitespace
            "http://host/%0Ainjected",  # cron treats % as a newline
            "http://host;rm -rf /",
            "http://host$(id)",
            "http://host`id`",
        ],
    )
    def test_rejects_shell_injection_payloads(self, payload):
        with pytest.raises(mod.SchedulerConfigError):
            mod.validate_base_url(payload)


class TestBuildCronLine:
    TAG = "# lab-tracker-daily-review"

    def test_builds_a_line_for_valid_inputs(self):
        line = mod.build_cron_line(
            interval="15",
            base_url="https://lab.example.org",
            trigger="/opt/lt/trigger.sh",
            log="/home/user/daily review.log",  # spaces are fine (double-quoted)
            tag=self.TAG,
        )
        assert line.startswith("*/15 * * * * ")
        assert 'LAB_TRACKER_BASE_URL="https://lab.example.org"' in line
        assert line.endswith(self.TAG)

    @pytest.mark.parametrize(
        "log",
        ['/t/x";id;"', "/t/x%0Aid", "/t/x`id`", "/t/x$(id)", "/t/x;id", "/t/x\nid"],
    )
    def test_rejects_unsafe_log_paths(self, log):
        with pytest.raises(mod.SchedulerConfigError):
            mod.build_cron_line(
                interval="15",
                base_url="http://127.0.0.1:8000",
                trigger="/opt/lt/trigger.sh",
                log=log,
                tag=self.TAG,
            )

    def test_rejects_unsafe_trigger_path(self):
        with pytest.raises(mod.SchedulerConfigError):
            mod.build_cron_line(
                interval="15",
                base_url="http://127.0.0.1:8000",
                trigger='/opt/lt/"; id; "',
                log="/t/lt.log",
                tag=self.TAG,
            )


class TestMergeCrontab:
    TAG = "# lab-tracker-daily-review"
    LINE = "*/15 * * * * trigger # lab-tracker-daily-review"

    def test_populated_crontab_dedupes_tag_and_preserves_others(self):
        existing = "0 9 * * * other-job\n*/5 * * * * stale # lab-tracker-daily-review\n"
        merged = mod.merge_crontab(existing, 0, "", tag=self.TAG, line=self.LINE)
        assert "other-job" in merged
        assert merged.count("lab-tracker-daily-review") == 1
        assert merged.rstrip().endswith(self.LINE)

    def test_absent_crontab_signal_is_treated_as_empty(self):
        merged = mod.merge_crontab("", 1, "no crontab for alice", tag=self.TAG, line=self.LINE)
        assert merged == self.LINE + "\n"

    def test_unexpected_read_failure_fails_closed(self):
        # A permission/transient error must NOT overwrite the crontab.
        with pytest.raises(mod.CrontabReadError):
            mod.merge_crontab(
                "keep-this-job", 1, "crontab: permission denied", tag=self.TAG, line=self.LINE
            )

    def test_fail_closed_via_subprocess_emits_nothing(self):
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT_PATH), "merge-crontab",
                "--exit-code", "1", "--stderr", "crontab: some other error",
                "--tag", self.TAG, "--line", self.LINE,
            ],
            input="EXISTING IMPORTANT JOB\n",
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert result.stdout == ""  # never prints a would-be-overwriting crontab
        assert "refusing to overwrite" in result.stderr


class TestRedactUrlCredentials:
    def test_masks_password(self):
        assert (
            mod.redact_url_credentials("postgresql+psycopg://user:s3cret@host:5432/db")
            == "postgresql+psycopg://user:***@host:5432/db"
        )

    def test_leaves_credential_free_url_untouched(self):
        assert mod.redact_url_credentials("sqlite:///lab.db") == "sqlite:///lab.db"
        assert mod.redact_url_credentials("http://127.0.0.1:8000") == "http://127.0.0.1:8000"

    def test_never_leaks_the_password_substring(self):
        redacted = mod.redact_url_credentials("postgres://admin:hunter2@db.example.org/lt")
        assert "hunter2" not in redacted
        assert "admin" in redacted  # username is not a secret

    @pytest.mark.parametrize(
        "url,secret",
        [
            ("postgresql+psycopg://lab:pa/ss@host:5432/db", "pa/ss"),  # '/' defeats urlsplit
            ("postgresql+psycopg://lab:pa?ss@host:5432/db", "pa?ss"),  # '?'
            ("postgresql+psycopg://lab:pa#ss@host:5432/db", "pa#ss"),  # '#'
            ("postgresql://host:5432/db?password=s3cr3t&sslmode=require", "s3cr3t"),  # query
            ("postgres://user:secret@host/db?token=secret", "secret"),  # reused in query
            ("postgresql://user:s3cret@[2001:db8::1]:5432/db", "s3cret"),  # IPv6 host
        ],
    )
    def test_masks_every_secret_shape(self, url, secret):
        redacted = mod.redact_url_credentials(url)
        assert "***" in redacted
        assert secret not in redacted

    def test_preserves_ipv6_host_brackets(self):
        redacted = mod.redact_url_credentials("postgresql://u:pw@[2001:db8::1]:5432/db")
        assert "[2001:db8::1]:5432" in redacted
        assert "pw" not in redacted.replace(":***@", "")


class TestLoginRequestBody:
    @pytest.mark.parametrize("secret", ADVERSARIAL_SECRETS)
    def test_adversarial_credentials_round_trip_as_valid_json(self, secret):
        body = mod.login_request_body("ad\"min", secret)
        parsed = json.loads(body)  # must be valid JSON, not injected/broken
        assert parsed == {"username": 'ad"min', "password": secret}


class TestLaunchdPlist:
    def test_plist_parses_and_does_not_source_or_embed_secrets(self):
        rendered = mod.render_launchd_plist(
            label="com.lab-tracker.daily-review",
            trigger="/opt/lt/trigger.sh",
            secrets_file="/home/alice/.config/lab-tracker/daily-review.secrets.json",
            base_url="https://lab.example.org/",
            interval_seconds=900,
            log="/home/alice/.lab-tracker.log",
        )
        document = plistlib.loads(rendered.encode("utf-8"))
        # Runs the trigger directly — never `sh -c '. env'` that would eval secrets.
        assert document["ProgramArguments"] == ["/opt/lt/trigger.sh"]
        assert document["StartInterval"] == 900
        # Only the secrets-file PATH and validated base URL ride in the plist,
        # never a secret value.
        assert document["EnvironmentVariables"] == {
            "LAB_TRACKER_SECRETS_FILE": "/home/alice/.config/lab-tracker/daily-review.secrets.json",
            "LAB_TRACKER_BASE_URL": "https://lab.example.org",
        }


class TestSecretsFile:
    @pytest.mark.parametrize("secret", ADVERSARIAL_SECRETS)
    def test_adversarial_secret_round_trips_without_evaluation(self, tmp_path, secret):
        path = str(tmp_path / "daily-review.secrets.json")
        mod.write_secrets_file(path, {"LAB_TRACKER_API_KEY": secret, "LAB_TRACKER_ADMIN_PASS": ""})
        # Stored as JSON, read back structurally — equal to input, never shell-eval'd.
        assert mod.read_secret(path, "LAB_TRACKER_API_KEY") == secret
        assert mod.read_secret(path, "LAB_TRACKER_ADMIN_PASS") == ""  # empty omitted -> ""
        with open(path, encoding="utf-8") as handle:
            assert json.load(handle) == {"LAB_TRACKER_API_KEY": secret}

    def test_secrets_file_is_private(self, tmp_path):
        path = str(tmp_path / "secrets.json")
        mod.write_secrets_file(path, {"LAB_TRACKER_API_KEY": "lpat_abc"})
        mode = stat.S_IMODE(Path(path).stat().st_mode)
        assert mode == 0o600

    def test_missing_secrets_file_reads_empty(self, tmp_path):
        assert mod.read_secret(str(tmp_path / "nope.json"), "LAB_TRACKER_API_KEY") == ""

    def test_refuses_to_follow_a_symlink(self, tmp_path):
        target = tmp_path / "victim"
        target.write_text("do not clobber")
        link = tmp_path / "daily-review.secrets.json"
        link.symlink_to(target)
        with pytest.raises(OSError):  # O_NOFOLLOW -> ELOOP
            mod.write_secrets_file(str(link), {"LAB_TRACKER_API_KEY": "lpat_abc"})
        # The symlink target was never truncated/overwritten.
        assert target.read_text() == "do not clobber"


class TestCronInstallerAdapter:
    """End-to-end tests of install-daily-review.sh with a stub crontab on PATH."""

    def _run(self, tmp_path, crontab_script: str):
        stub_dir = tmp_path / "bin"
        stub_dir.mkdir()
        crontab = stub_dir / "crontab"
        crontab.write_text(crontab_script)
        crontab.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "LAB_TRACKER_DAILY_REVIEW_LOG": str(tmp_path / "daily-review.log"),
        }
        return subprocess.run(
            ["sh", str(CRON_INSTALLER), "15", "http://127.0.0.1:8000"],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_fail_closed_when_crontab_read_errors(self, tmp_path):
        installed = tmp_path / "installed-crontab"
        script = (
            "#!/bin/sh\n"
            'if [ "$1" = "-l" ]; then echo "crontab: permission denied" >&2; exit 1; fi\n'
            f'cat > "{installed}"\n'
        )
        result = self._run(tmp_path, script)
        assert result.returncode != 0
        # The crontab was never overwritten despite the read error.
        assert not installed.exists()

    def test_no_existing_crontab_installs_just_the_line(self, tmp_path):
        installed = tmp_path / "installed-crontab"
        script = (
            "#!/bin/sh\n"
            'if [ "$1" = "-l" ]; then echo "no crontab for tester" >&2; exit 1; fi\n'
            f'cat > "{installed}"\n'
        )
        result = self._run(tmp_path, script)
        assert result.returncode == 0, result.stderr
        content = installed.read_text()
        assert content.count("lab-tracker-daily-review") == 1

    def test_success_merges_dedupes_tag_and_preserves_other_jobs(self, tmp_path):
        installed = tmp_path / "installed-crontab"
        script = (
            "#!/bin/sh\n"
            'if [ "$1" = "-l" ]; then '
            "printf '0 9 * * * unrelated-job\\n"
            "*/5 * * * * stale # lab-tracker-daily-review\\n'; exit 0; fi\n"
            f'cat > "{installed}"\n'
        )
        result = self._run(tmp_path, script)
        assert result.returncode == 0, result.stderr
        content = installed.read_text()
        assert "unrelated-job" in content
        assert content.count("lab-tracker-daily-review") == 1
