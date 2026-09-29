"""Credential scrubbing for text a capture copies into an event -- the one redactor.

Commands (``lt run`` argv, ``dvc.lock`` ``cmd``, agent session shell commands),
pipeline and scheduler logs, and error messages routinely echo credentials:
an ``Authorization`` header, a ``--token abc`` argument, ``password=...`` in a
connection string, ``https://user:secret@host`` in a URL, or a bare API key.
:func:`redact_capture_text` removes those before the text reaches an outbox
event, a rendered note, or note metadata, and every client capture path calls
it (``lt run`` per argv element through its argv-aware ``redact_argv``, and
``agent_session.redact_secrets`` as a thin wrapper).

What goes (each replaced by :data:`REDACTED`):

* the literal values of the client's own credential variables
  (:data:`SECRET_ENV_VARS`) and any extra ``secrets``, also URL-encoded;
* PEM private-key blocks;
* URL credentials: ``https://user:pw@host`` loses all userinfo (a bare user can
  be a token); an ssh login is addressing and stays, its password goes;
* query values whose key names a secret (``?token=``, ``&api_key=``, ``sig``,
  ``signature``, ``X-Amz-Signature``, ``X-Goog-Signature``,
  ``X-Amz-Credential``, ``X-Amz-Security-Token``, ...), also URL-encoded;
* the whole value of credential headers (``Authorization`` incl. ``Basic``
  and ``token x``, ``Proxy-Authorization``, ``Cookie``/``Set-Cookie``,
  ``X-Api-Key``, ``PRIVATE-TOKEN``, ``X-Vault-Token``, ``X-Auth-Token``) and
  ``Bearer`` tokens anywhere;
* ``-u``/``--user user:pw`` (the user stays);
* the value of ``--flag value``/``--flag=value`` and of ``name=value``,
  ``name: value`` and JSON ``"name": "value"`` pairs whose NAME ENDS in a
  secret word (:func:`looks_secret_name`), so ``max_tokens``, ``--tokenizer``,
  ``token_count`` or ``--num-pass`` survive;
* ``mysql -p<pw>`` (attached form only: ``mysql -p db`` prompts),
  ``sshpass -p <pw>`` and ``docker login -p <pw>``;
* well-known token shapes: GitHub, GitLab, Slack, AWS ``AKIA``/``ASIA``,
  ``sk-`` keys, Stripe ``sk_``/``rk_`` live/test, Google ``AIza``, Hugging
  Face ``hf_``, JWTs, and Lab Tracker's own bearer tokens.

It errs on the side of over-redaction: captured text is context for a
reviewer, never a record that must be byte-exact. It is a filter for the
obvious cases, not a guarantee.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus

from lab_tracker.provider_error_redaction import REDACTED

# Environment variables whose values are credentials the client itself holds.
SECRET_ENV_VARS = (
    "LAB_TRACKER_ACCESS_TOKEN",
    "LAB_TRACKER_TOKEN",
    "LAB_TRACKER_PASSWORD",
    "LAB_TRACKER_MCP_PASSWORD",
    "LAB_TRACKER_MCP_TOKEN",
    "LAB_TRACKER_MCP_API_KEY",
    "LAB_TRACKER_API_KEY",
)
# A literal secret shorter than this is never searched for (it would match noise).
_MIN_LITERAL_SECRET_CHARS = 4

# --- secret names -------------------------------------------------------------

# A name is secret when its LAST part is one of these words ...
_SECRET_NAME_WORDS = frozenset(
    {
        "password",
        "passwords",
        "passwd",
        "passphrase",
        "pwd",
        "secret",
        "secrets",
        "token",
        "apikey",
        "credential",
        "credentials",
        "creds",
        "auth",
        "authorization",
        "cookie",
    }
)
# ... or its last part ends in one of these (``PGPASSWORD``, ``accessToken``,
# ``clientSecret``, ``secretAccessKey``) ...
_SECRET_NAME_SUFFIXES = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "apikey",
    "accesskey",
    "secretkey",
    "privatekey",
    "sessionkey",
    "signingkey",
    "masterkey",
)
# ... or it ends in ``<qualifier>-key`` (``--api-key``, ``AWS_ACCESS_KEY``).
_KEY_QUALIFIERS = frozenset(
    {
        "api",
        "access",
        "secret",
        "private",
        "signing",
        "encryption",
        "client",
        "master",
        "account",
        "app",
        "consumer",
        "license",
        "service",
        "session",
        "storage",
        "sas",
        "shared",
        "subscription",
    }
)
# Query keys that carry a secret without a secret-sounding name.
_SECRET_QUERY_KEYS = frozenset(
    {"key", "sig", "signature", "code", "x-amz-signature", "x-goog-signature"}
)


def looks_secret_name(name: str) -> bool:
    """True when a flag, key, header or query name (``api-key``, ``DB_PASSWORD``) names a secret.

    The name must END in the secret word: ``max_tokens``, ``--tokenizer``,
    ``token_count``, ``--password-file`` and ``--num-pass`` are not secrets,
    and a negation (``--no-password``) never is.
    """

    normalized = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    if not normalized:
        return False
    parts = normalized.split("-")
    if parts[0] == "no" and len(parts) > 1:
        return False
    last = parts[-1]
    if last in _SECRET_NAME_WORDS or last.endswith(_SECRET_NAME_SUFFIXES):
        return True
    return last == "key" and len(parts) > 1 and parts[-2] in _KEY_QUALIFIERS


def _looks_secret_query_key(name: str) -> bool:
    return looks_secret_name(name) or name.lower() in _SECRET_QUERY_KEYS


# --- patterns -----------------------------------------------------------------

_NOT_REDACTED = rf"(?!{re.escape(REDACTED)})"
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_URL = re.compile(r"\b(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<rest>[^\s'\"<>`]+)")
_SSH_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git"})
# ``?key=value`` / ``&key=value`` anywhere (inside or outside a URL).
_QUERY_PAIR = re.compile(r"(?P<sep>[?&;])(?P<key>[A-Za-z0-9_.\-]+)=(?P<value>[^&#\s\"'<>;]*)")
_ENCODED_QUERY_PAIR = re.compile(
    r"(?P<sep>%3[fF]|%26)(?P<key>[A-Za-z0-9_.\-]+)(?P<eq>=|%3[dD])(?P<value>.*?)(?=%26|[\s\"'<>&]|$)"
)
# Credential headers: the whole value to the end of the line (or a quote).
_HEADER = re.compile(
    r"(?<![\w-])(?P<name>proxy-authorization|authorization|set-cookie|cookie|x-goog-api-key"
    r"|x-api-key|api-key|private-token|x-vault-token|x-auth-token|x-amz-security-token)"
    rf"(?P<sep>[ \t]*:[ \t]*){_NOT_REDACTED}(?P<value>[^\r\n'\"]+)",
    re.IGNORECASE,
)
_BEARER = re.compile(rf"\b(?P<scheme>Bearer)\s+{_NOT_REDACTED}[A-Za-z0-9._~+/=-]{{8,}}", re.I)
# ``curl -u user:pw`` / ``--user=user:pw``: the user stays.
_USER_PASSWORD = re.compile(
    r"(?<![\w-])(?P<flag>-u|--user)(?P<sep>[ \t]+|=)"
    rf"(?P<user>[^\s:'\"]+):{_NOT_REDACTED}(?P<value>[^\s'\"]+)"
)
_QUOTED_OR_BARE = r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']+)"
# ``--name=value`` or ``--name value`` (a value never starts with ``-``).
_FLAG = re.compile(
    r"(?<![\w-])(?P<flag>--?(?P<name>[A-Za-z][A-Za-z0-9_.-]*))"
    rf"(?:(?P<eq>=){_NOT_REDACTED}(?P<value_eq>{_QUOTED_OR_BARE})"
    rf"|(?P<ws>[ \t]+)(?!-){_NOT_REDACTED}(?P<value_ws>{_QUOTED_OR_BARE}))"
)
# ``name=value``, ``name: value``, ``"name": "value"``.
_ASSIGNMENT = re.compile(
    r"(?<![\w.\-/])(?P<q1>[\"']?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)(?P<q2>[\"']?)"
    rf"(?P<sep>[ \t]*[=:][ \t]*){_NOT_REDACTED}"
    r"(?P<value>\"[^\"\n]*\"|'[^'\n]*'|[^\s,;\"'}\])]+)"
)
# Password flags that are only unambiguous for these commands.
_MYSQL_ATTACHED = re.compile(
    r"(?P<head>\b(?:mysql|mysqladmin|mysqldump|mariadb)\b[^\n;|&]*?[ \t]-p)"
    rf"{_NOT_REDACTED}(?P<value>[^\s'\"]+)"
)
_SSHPASS = re.compile(rf"(?P<head>\bsshpass[ \t]+-p[ \t]*){_NOT_REDACTED}(?P<value>[^\s'\"]+)")
_DOCKER_LOGIN = re.compile(
    r"(?P<head>\bdocker[ \t]+login\b[^\n;|&]*?[ \t]-p[ \t]*)"
    rf"{_NOT_REDACTED}(?P<value>[^\s'\"]+)"
)
_TOKEN_SHAPES = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|hf_[A-Za-z0-9]{20,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|sk-(?:ant-|proj-)?[A-Za-z0-9_-]{12,}"
    r"|[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|AIza[0-9A-Za-z_-]{20,}"
    r"|(?:linv|lpat|ldev|lpair)_[0-9A-Za-z._~-]{4,})"
)


def redact_capture_text(text: str, *, secrets: tuple[str, ...] = ()) -> str:
    """Return ``text`` with credential material replaced by ``[REDACTED]``.

    See the module docstring for everything that is removed. ``secrets`` adds
    literal values to remove (with the client's own credential variables).
    """

    if not text:
        return text
    cleaned = _replace_literals(text, _known_secrets(secrets))
    cleaned = _PRIVATE_KEY.sub(REDACTED, cleaned)
    cleaned = _URL.sub(_redact_url, cleaned)
    cleaned = _HEADER.sub(lambda m: f"{m['name']}{m['sep']}{REDACTED}", cleaned)
    cleaned = _BEARER.sub(lambda m: f"{m['scheme']} {REDACTED}", cleaned)
    cleaned = _USER_PASSWORD.sub(lambda m: f"{m['flag']}{m['sep']}{m['user']}:{REDACTED}", cleaned)
    for pattern in (_MYSQL_ATTACHED, _SSHPASS, _DOCKER_LOGIN):
        cleaned = pattern.sub(lambda m: f"{m['head']}{REDACTED}", cleaned)
    # Query pairs before assignments, so ``?api_key=x&page=2`` keeps ``&page=2``.
    cleaned = _redact_query(cleaned)
    cleaned = _FLAG.sub(_redact_flag, cleaned)
    cleaned = _ASSIGNMENT.sub(_redact_assignment, cleaned)
    return _TOKEN_SHAPES.sub(REDACTED, cleaned)


def _known_secrets(extra: Iterable[str]) -> list[str]:
    values = {
        value.strip()
        for value in (*extra, *(os.getenv(name) or "" for name in SECRET_ENV_VARS))
        if len(value.strip()) >= _MIN_LITERAL_SECRET_CHARS
    }
    return sorted(values, key=len, reverse=True)


def _replace_literals(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        for form in {secret, quote(secret, safe=""), quote_plus(secret, safe="")}:
            if form:
                text = text.replace(form, REDACTED)
    return text


def _redact_url(match: re.Match[str]) -> str:
    scheme, rest = match["scheme"], match["rest"]
    split = re.search(r"[/?#]", rest)
    authority, tail = (rest[: split.start()], rest[split.start() :]) if split else (rest, "")
    userinfo, at, host = authority.rpartition("@")
    if at and userinfo != REDACTED:
        login, colon, _password = userinfo.partition(":")
        if scheme.lower() in _SSH_SCHEMES and login:
            userinfo = f"{login}:{REDACTED}" if colon else login
        else:
            userinfo = REDACTED
        authority = f"{userinfo}@{host}"
    return f"{scheme}://{authority}{_redact_query(tail)}"


def _redact_query(text: str) -> str:
    text = _QUERY_PAIR.sub(
        lambda m: (
            f"{m['sep']}{m['key']}={REDACTED}"
            if _looks_secret_query_key(m["key"]) and m["value"] and m["value"] != REDACTED
            else m.group(0)
        ),
        text,
    )
    return _ENCODED_QUERY_PAIR.sub(
        lambda m: (
            f"{m['sep']}{m['key']}{m['eq']}{REDACTED}"
            if _looks_secret_query_key(m["key"]) and m["value"] and m["value"] != REDACTED
            else m.group(0)
        ),
        text,
    )


def _redact_flag(match: re.Match[str]) -> str:
    if not looks_secret_name(match["name"]):
        return match.group(0)
    if match["eq"]:
        return f"{match['flag']}={REDACTED}"
    return f"{match['flag']}{match['ws']}{REDACTED}"


def _redact_assignment(match: re.Match[str]) -> str:
    if not looks_secret_name(match["name"]):
        return match.group(0)
    return f"{match['q1']}{match['name']}{match['q2']}{match['sep']}{REDACTED}"


__all__ = ["REDACTED", "SECRET_ENV_VARS", "looks_secret_name", "redact_capture_text"]
