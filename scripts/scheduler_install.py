#!/usr/bin/env python3
"""Tested implementation behind the daily-review scheduler installers.

The shell/PowerShell installers are thin adapters over this module so the
security-sensitive logic lives in one place that is unit-tested in Python
rather than trusted to shell text:

* fail-closed crontab merging (an unexpected ``crontab -l`` failure never
  overwrites unrelated jobs),
* schedule/base-URL validation,
* credentials serialized structurally (JSON) and never shell-evaluated, and
* credential-aware URL redaction for logs.

Pure stdlib only, so cron/launchd can run it with the system ``python3``
regardless of any virtualenv. Subcommands emit results on stdout and exit
non-zero on error; the pure functions are unit-tested directly.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import sys
from urllib.parse import urlsplit

MAX_INTERVAL_MINUTES = 24 * 60
_CRONTAB_ABSENT_MARKERS = ("no crontab for",)
# A base API URL only ever needs scheme, host, optional port, and an optional
# path prefix. Restricting to this charset rejects every byte that is dangerous
# in a crontab command field (quotes, ; $ % ( ) { } < > | & backtick, control
# chars) BEFORE it is interpolated, closing the shell-injection vector.
_SAFE_BASE_URL = re.compile(r"[A-Za-z0-9._:/\-]+")
# Bytes that would break out of, or be re-interpreted inside, a double-quoted
# crontab command field (paths may legitimately contain spaces, so those stay).
_CRON_FIELD_FORBIDDEN = set('"\'`$\\%;\r\n') | {chr(code) for code in range(0x20)}
# scheme://user:password@  — password matches anything up to the '@' so raw
# '/', '?', '#' in a pasted DB password are handled (urlsplit mis-parses those).
_URL_USERINFO = re.compile(
    r"(?P<prefix>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^:@/?#]+):(?P<pw>[^@]*)@"
)
_URL_QUERY_SECRET = re.compile(
    r"(?P<sep>[?&])(?P<key>password|sslpassword)=(?P<val>[^&#]*)", re.IGNORECASE
)


class SchedulerConfigError(ValueError):
    """A schedule/base-URL/scope input was invalid."""


class CrontabReadError(RuntimeError):
    """``crontab -l`` failed for a reason other than an absent crontab."""


def validate_interval(value: object) -> int:
    """Return a positive integer minute interval or raise SchedulerConfigError."""
    try:
        minutes = int(str(value).strip())
    except (TypeError, ValueError):
        raise SchedulerConfigError(
            f"Interval must be a positive integer number of minutes, got {value!r}."
        ) from None
    if minutes <= 0 or minutes > MAX_INTERVAL_MINUTES:
        raise SchedulerConfigError(
            f"Interval must be between 1 and {MAX_INTERVAL_MINUTES} minutes, got {minutes}."
        )
    return minutes


def validate_base_url(value: object) -> str:
    """Return a normalized http(s) base URL or raise SchedulerConfigError.

    Enforces a strict charset in addition to the http(s) shape so a value like
    ``http://a";touch${IFS}/x;"`` (which urlsplit happily accepts and which has
    no literal whitespace) can never reach the crontab command field.
    """
    text = str(value or "").strip()
    if not text:
        raise SchedulerConfigError(f"Base URL must not be empty, got {value!r}.")
    if not _SAFE_BASE_URL.fullmatch(text):
        raise SchedulerConfigError(
            f"Base URL may only contain letters, digits, and ._:/- , got {value!r}."
        )
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SchedulerConfigError(f"Base URL must be an http(s) URL, got {value!r}.")
    return text.rstrip("/")


def validate_cron_field(value: str, *, name: str) -> str:
    """Reject bytes that would break out of a double-quoted crontab command field.

    Paths (trigger, log) may contain spaces, but a quote, backtick, ``$``, ``\\``,
    ``%`` (cron turns it into a newline), ``;`` or a control character would let
    the value escape its quotes or split the cron line, so those are refused.
    """
    text = str(value)
    bad = sorted({ch for ch in text if ch in _CRON_FIELD_FORBIDDEN})
    if bad:
        raise SchedulerConfigError(
            f"{name} contains characters unsafe for a crontab command field: {bad!r}."
        )
    return text


def _looks_like_absent_crontab(stderr: str) -> bool:
    lowered = (stderr or "").lower()
    return any(marker in lowered for marker in _CRONTAB_ABSENT_MARKERS)


def merge_crontab(
    existing_stdout: str,
    exit_code: int,
    existing_stderr: str,
    *,
    tag: str,
    line: str,
) -> str:
    """Fail-closed crontab merge.

    ``crontab -l`` exits non-zero both when there is no crontab and on real read
    failures. Only the recognized 'no crontab for' signal is treated as empty;
    any other non-zero exit raises CrontabReadError so a transient or permission
    error can never wipe unrelated cron jobs (fail closed, not open).
    """
    if exit_code == 0:
        existing = existing_stdout
    elif _looks_like_absent_crontab(existing_stderr):
        existing = ""
    else:
        raise CrontabReadError(
            f"crontab read failed (exit {exit_code}); refusing to overwrite the crontab. "
            f"stderr: {existing_stderr.strip()}"
        )
    kept = [entry for entry in existing.splitlines() if tag not in entry]
    kept.append(line)
    return "\n".join(kept) + "\n"


def build_cron_line(
    *,
    interval: object,
    base_url: object,
    trigger: str,
    secrets_file: str,
    log: str,
    tag: str,
) -> str:
    """Build a validated cron line for the daily-review trigger.

    Every interpolated field is validated first: the interval is an integer, the
    base URL passes a strict charset, and the trigger/secrets/log paths cannot
    contain a byte that would break out of their double quotes or split the cron
    line.
    """
    minutes = validate_interval(interval)
    url = validate_base_url(base_url)
    safe_trigger = validate_cron_field(trigger, name="Trigger path")
    safe_secrets_file = validate_cron_field(secrets_file, name="Secrets-file path")
    safe_log = validate_cron_field(log, name="Log path")
    return (
        f'*/{minutes} * * * * LAB_TRACKER_BASE_URL="{url}" '
        f'LAB_TRACKER_SECRETS_FILE="{safe_secrets_file}" '
        f'"{safe_trigger}" >> "{safe_log}" 2>&1 {tag}'
    )


def redact_url_credentials(url: str) -> str:
    """Mask the password in a database/connection URL for safe logging.

    Handles passwords in the userinfo slot (including raw ``/ ? #`` that defeat
    urlsplit), passwords passed as ``?password=`` query parameters, and the same
    secret reused elsewhere in the URL, while leaving usernames, hosts (including
    bracketed IPv6), and credential-free URLs untouched.
    """
    text = str(url or "")
    match = _URL_USERINFO.search(text)
    if match:
        password = match.group("pw")
        text = _URL_USERINFO.sub(
            lambda m: f"{m.group('prefix')}{m.group('user')}:***@", text
        )
        if password:
            # Mask any other occurrence (e.g. the same secret duplicated in a
            # query parameter) now that we know the value.
            text = text.replace(password, "***")
    text = _URL_QUERY_SECRET.sub(lambda m: f"{m.group('sep')}{m.group('key')}=***", text)
    return text


def login_request_body(username: str, password: str) -> str:
    """Serialize an auth/login body as JSON (never via shell interpolation)."""
    return json.dumps({"username": username, "password": password})


def render_launchd_plist(
    *,
    label: str,
    trigger: str,
    secrets_file: str,
    base_url: str,
    interval_seconds: int,
    log: str,
) -> str:
    """Render a LaunchAgent plist that runs the trigger WITHOUT sourcing secrets.

    Secrets stay in a 0600 JSON file the trigger reads structurally; the plist
    carries only the (non-secret) secrets-file path and validated base URL, never
    the secret values, and never a ``sh -c '. env'`` that would shell-evaluate
    them. Values are plistlib/XML-escaped, so they cannot break the document.
    """
    document = {
        "Label": label,
        "ProgramArguments": [trigger],
        "EnvironmentVariables": {
            "LAB_TRACKER_SECRETS_FILE": secrets_file,
            "LAB_TRACKER_BASE_URL": validate_base_url(base_url),
        },
        "StartInterval": int(interval_seconds),
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    return plistlib.dumps(document).decode("utf-8")


def write_secrets_file(path: str, values: dict[str, str]) -> None:
    """Write non-empty secret values to a private 0600 JSON file.

    O_NOFOLLOW refuses to follow a pre-planted symlink at the fixed secrets path,
    so a local attacker cannot redirect the write (or the O_TRUNC) onto another
    file the user owns.
    """
    data = {key: value for key, value in values.items() if value}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.chmod(path, 0o600)


def read_secret(path: str, key: str) -> str:
    """Read a single secret value from the JSON secrets file (never eval'd)."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return ""
    value = data.get(key, "")
    return "" if value is None else str(value)


def _cmd_merge_crontab(args: argparse.Namespace) -> int:
    existing = sys.stdin.read()
    merged = merge_crontab(
        existing, args.exit_code, args.stderr, tag=args.tag, line=args.line
    )
    sys.stdout.write(merged)
    return 0


def _cmd_cron_line(args: argparse.Namespace) -> int:
    sys.stdout.write(
        build_cron_line(
            interval=args.interval,
            base_url=args.base_url,
            trigger=args.trigger,
            secrets_file=args.secrets_file,
            log=args.log,
            tag=args.tag,
        )
        + "\n"
    )
    return 0


def _cmd_redact_url(args: argparse.Namespace) -> int:
    sys.stdout.write(redact_url_credentials(args.url) + "\n")
    return 0


def _cmd_login_body(_args: argparse.Namespace) -> int:
    sys.stdout.write(
        login_request_body(
            os.environ.get("LAB_TRACKER_ADMIN_USER", ""),
            os.environ.get("LAB_TRACKER_ADMIN_PASS", ""),
        )
    )
    return 0


def _cmd_render_plist(args: argparse.Namespace) -> int:
    sys.stdout.write(
        render_launchd_plist(
            label=args.label,
            trigger=args.trigger,
            secrets_file=args.secrets_file,
            base_url=args.base_url,
            interval_seconds=args.interval_seconds,
            log=args.log,
        )
    )
    return 0


def _cmd_write_secrets(args: argparse.Namespace) -> int:
    write_secrets_file(
        args.path,
        {
            "LAB_TRACKER_API_KEY": os.environ.get("LAB_TRACKER_API_KEY", ""),
            "LAB_TRACKER_ADMIN_USER": os.environ.get("LAB_TRACKER_ADMIN_USER", ""),
            "LAB_TRACKER_ADMIN_PASS": os.environ.get("LAB_TRACKER_ADMIN_PASS", ""),
        },
    )
    return 0


def _cmd_read_secret(args: argparse.Namespace) -> int:
    sys.stdout.write(read_secret(args.path, args.key))
    return 0


def _validate_interval_cmd(args: argparse.Namespace) -> int:
    sys.stdout.write(f"{validate_interval(args.interval)}\n")
    return 0


def _validate_base_url_cmd(args: argparse.Namespace) -> int:
    sys.stdout.write(f"{validate_base_url(args.base_url)}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("merge-crontab", help="Fail-closed crontab merge (stdin = crontab -l)")
    p.add_argument("--exit-code", type=int, required=True)
    p.add_argument("--stderr", default="")
    p.add_argument("--tag", required=True)
    p.add_argument("--line", required=True)
    p.set_defaults(func=_cmd_merge_crontab)

    p = sub.add_parser("cron-line", help="Build a validated cron line")
    p.add_argument("--interval", required=True)
    p.add_argument("--base-url", required=True)
    p.add_argument("--trigger", required=True)
    p.add_argument("--secrets-file", required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--tag", required=True)
    p.set_defaults(func=_cmd_cron_line)

    p = sub.add_parser("redact-url", help="Redact credentials in a URL")
    p.add_argument("url")
    p.set_defaults(func=_cmd_redact_url)

    p = sub.add_parser("login-body", help="Emit a JSON login body from env")
    p.set_defaults(func=_cmd_login_body)

    p = sub.add_parser("render-plist", help="Render a LaunchAgent plist")
    p.add_argument("--label", required=True)
    p.add_argument("--trigger", required=True)
    p.add_argument("--secrets-file", required=True)
    p.add_argument("--base-url", required=True)
    p.add_argument("--interval-seconds", type=int, required=True)
    p.add_argument("--log", required=True)
    p.set_defaults(func=_cmd_render_plist)

    p = sub.add_parser("write-secrets", help="Write a 0600 JSON secrets file from env")
    p.add_argument("path")
    p.set_defaults(func=_cmd_write_secrets)

    p = sub.add_parser("read-secret", help="Read one secret value from the JSON secrets file")
    p.add_argument("path")
    p.add_argument("key")
    p.set_defaults(func=_cmd_read_secret)

    p = sub.add_parser("validate-interval", help="Validate and echo an interval")
    p.add_argument("interval")
    p.set_defaults(func=_validate_interval_cmd)

    p = sub.add_parser("validate-base-url", help="Validate and echo a base URL")
    p.add_argument("base_url")
    p.set_defaults(func=_validate_base_url_cmd)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SchedulerConfigError, CrontabReadError) as error:
        sys.stderr.write(f"lab-tracker scheduler: {error}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
