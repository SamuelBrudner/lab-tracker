"""One redactor, three entry points, one table.

Every client capture path that copies text or a command line into an event
goes through :mod:`lab_tracker_client.redaction`: pipeline/HPC logs and
commands call ``redact_capture_text`` directly, ``agent_session.redact_secrets``
is a thin wrapper, and ``lt run``'s argv-aware ``redact_argv`` runs it on each
argument. The same cases must hold for all three.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from lab_tracker_client import agent_session
from lab_tracker_client.redaction import (
    REDACTED,
    SECRET_ENV_VARS,
    looks_secret_name,
    redact_capture_text,
)
from lab_tracker_client.run_capture import display_command, redact_argv

PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEsecretbody\n-----END RSA PRIVATE KEY-----"


@dataclass(frozen=True)
class Leak:
    """Text whose ``secrets`` must not survive, while ``keep`` must."""

    id: str
    text: str
    secrets: tuple[str, ...]
    keep: tuple[str, ...] = ()
    # The argv the ``lt run`` entry point sees; defaults to ``shlex.split(text)``.
    argv: tuple[str, ...] | None = None
    forbid: tuple[str, ...] = field(default=("]]", "--api-key:"))


LEAKS = [
    # Private keys and token shapes.
    Leak("pem", f"key:\n{PEM}", ("MIIEsecretbody",), argv=("deploy", "--cert", PEM)),
    Leak("hf", "use hf_abcdefghijklmnopqrstuvwxyz0123 now", ("hf_abcdefghijklmnop",)),
    Leak(
        "jwt",
        "id eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
        ("eyJzdWIiOiIxMjM0NTY3ODkwIn0",),
    ),
    Leak("aws-asia", "creds ASIAABCDEFGHIJKLMNOP", ("ASIAABCDEFGHIJKLMNOP",)),
    Leak("aws-akia", "creds AKIAABCDEFGHIJKLMNOP", ("AKIAABCDEFGHIJKLMNOP",)),
    Leak("stripe-live", "pay sk_live_" + "a1" * 12, ("a1a1a1a1a1a1",)),
    Leak("stripe-test", "refund rk_test_" + "b2" * 10, ("b2b2b2b2b2b2",)),
    Leak("sk-key", "export KEYVAL=sk-proj-abcdefghijklmnop", ("abcdefghijklmnop",)),
    Leak("google", "maps AIza" + "B" * 35, ("BBBBBBBBBBBBBBBBBBBB",)),
    Leak("github", "push ghp_" + "c" * 36, ("cccccccccccccccccccc",)),
    Leak("github-pat", "push github_pat_" + "d" * 30, ("dddddddddddddddddddd",)),
    Leak("gitlab", "push glpat-" + "e" * 22, ("eeeeeeeeeeeeeeeeeeee",)),
    Leak("slack", "notify xoxb-123456789012-abcdefghij", ("abcdefghij",)),
    Leak("lab-tracker", "lt setup connect --token lpat_abc.def123", ("lpat_abc.def123",)),
    # Credential headers: the whole value goes.
    *(
        Leak(
            f"header-{name}",
            f"curl -H '{header}' https://api.example.org",
            (secret,),
            keep=("https://api.example.org",),
        )
        for name, header, secret in (
            ("bearer", "Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
            ("basic", "Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
            ("token", "Authorization: token abc123def456", "abc123def456"),
            ("proxy", "Proxy-Authorization: Basic Zm9vOmJhcg==", "Zm9vOmJhcg"),
            ("cookie", "Cookie: session=abc123xyz; theme=dark", "abc123xyz"),
            ("x-api-key", "X-Api-Key: xyz789secret", "xyz789secret"),
            ("private-token", "PRIVATE-TOKEN: glsecret123abc", "glsecret123abc"),
            ("vault", "X-Vault-Token: hvs.vaultsecret", "hvs.vaultsecret"),
            ("x-auth-token", "X-Auth-Token: authsecret99", "authsecret99"),
        )
    ),
    # Query values for secret keys.
    Leak(
        "s3-presigned",
        "curl 'https://bucket.s3.amazonaws.com/k?X-Amz-Algorithm=AWS4"
        "&X-Amz-Credential=AKIDEXAMPLE%2F2024&X-Amz-Signature=abcdef123456"
        "&X-Amz-Security-Token=sectok789&X-Amz-Date=20240101'",
        ("AKIDEXAMPLE", "abcdef123456", "sectok789"),
        keep=("X-Amz-Algorithm=AWS4", "X-Amz-Date=20240101"),
    ),
    Leak(
        "gcs-signed",
        "wget https://storage.googleapis.com/b/o?X-Goog-Signature=googsig123&alt=media",
        ("googsig123",),
        keep=("alt=media",),
    ),
    Leak(
        "azure-sas",
        "az copy https://acct.blob.core.windows.net/c/b?sv=2020-08-04&sig=azuresig123",
        ("azuresig123",),
        keep=("sv=2020-08-04",),
    ),
    Leak("query-api-key", "GET /x?api_key=abc123secret&page=2", ("abc123secret",), ("page=2",)),
    Leak(
        "query-token",
        "fetch https://api.example.org/v1?token=qtok123&page=2",
        ("qtok123",),
        keep=("page=2",),
    ),
    # Credentialed URLs.
    Leak(
        "url-userinfo",
        "git clone https://user:pa55word@github.com/lab/repo.git",
        ("pa55word",),
        keep=("github.com/lab/repo.git",),
    ),
    Leak(
        "url-bare-token",
        "git clone https://x-access-token-value@github.com/lab/repo.git",
        ("x-access-token-value",),
    ),
    Leak(
        "ssh-password",
        "git fetch ssh://git:sshpw123@host.example/repo",
        ("sshpw123",),
        keep=("ssh://git:",),
    ),
    # -u/--user user:password keeps the user.
    Leak("curl-u", "curl -u alice:hunter2 https://example.org", ("hunter2",), ("alice",)),
    Leak("curl-user", "curl --user=bob:pw12345 https://example.org", ("pw12345",), ("bob",)),
    # Secret flags.
    Leak("flag-space", "tool --token s3cr3tval --verbose", ("s3cr3tval",), ("--verbose",)),
    Leak("flag-equals", "tool --api-key=xyz123secret run", ("xyz123secret",), ("--api-key=",)),
    Leak("flag-mysql-long", "mysql --password s3cretvalue db", ("s3cretvalue",)),
    Leak("flag-single-dash", "java -password hunter22 -jar app.jar", ("hunter22",), ("-jar",)),
    Leak("flag-client-secret", "python fit.py --client-secret cs123456", ("cs123456",)),
    # name=value / name: value / JSON pairs whose name ENDS in a secret word.
    Leak("assign-equals", "password=hunter2 user=ada", ("hunter2",), ("user=ada",)),
    Leak("assign-colon", "api_key: 'abc123'", ("abc123",), argv=("echo", "api_key: 'abc123'")),
    Leak(
        "assign-json",
        '{"access_token": "tok-123456", "expires": 30}',
        ("tok-123456",),
        keep=('"expires": 30',),
        argv=("curl", "-d", '{"access_token": "tok-123456", "expires": 30}'),
    ),
    Leak("assign-env-api-key", "export OPENAI_API_KEY=abcdefghijklmnopq", ("abcdefghijklmnopq",)),
    Leak("assign-env-token", "GITHUB_TOKEN=plainvalue123 make", ("plainvalue123",), ("make",)),
    Leak(
        "assign-prose",
        "db password: correct-horse",
        ("correct-horse",),
        argv=("echo", "db password: correct-horse"),
    ),
    Leak("assign-pgpassword", "PGPASSWORD=pgpw123 psql", ("pgpw123",), ("psql",)),
    Leak(
        "assign-hydra",
        "python train.py db.password=hydrapw +trainer.api_key=hydrakey",
        ("hydrapw", "hydrakey"),
    ),
    Leak("assign-pwd", "DB_PWD=dbpw123 ./run", ("dbpw123",)),
    Leak("assign-aws", "AWS_SECRET_ACCESS_KEY=awssecret123 aws s3 ls", ("awssecret123",)),
    # Password flags only some commands have.
    Leak("mysql-attached", "mysql -u root -phunter2 mydb", ("hunter2",), ("mydb", "root")),
    Leak("mysqldump-attached", "mysqldump -pdumppw db", ("dumppw",)),
    Leak("sshpass", "sshpass -p sshpw123 ssh host", ("sshpw123",), ("ssh host",)),
    Leak(
        "docker-login",
        "docker login -u ci -p dockpw123 registry.example.org",
        ("dockpw123",),
        keep=("registry.example.org",),
    ),
]

SAFE = [
    "client.messages.create(max_tokens=100)",
    "python train.py --max-tokens 512 --tokenizer bert --num-pass 3",
    "token_count: 5",
    "tokens: 5",
    "edit src/token_store.py and tests/test_passwords.py",
    "see https://example.org/docs/page?lang=en&page=2",
    "git clone ssh://git@github.com/lab/repo.git",
    "python fit.py --password-file pw.txt --no-password --token-name ci --author Ada",
    "mysql -u root -p mydb",
    "python -m pytest -p no:cacheprovider",
    "python -u train.py",
    "ls -p /tmp",
    "docker run -p 8080:80 nginx",
    "git commit -m 'fix the auth flow'",
    "export PATH=/usr/bin:/bin",
    "pip install scikit-learn",
]


def _text(text: str) -> str:
    return redact_capture_text(text)


def _agent(text: str) -> str:
    return agent_session.redact_secrets(text)


def _argv(argv: tuple[str, ...]) -> str:
    return display_command(redact_argv(list(argv)))


TEXT_ENTRY_POINTS: dict[str, Callable[[str], str]] = {
    "redact_capture_text": _text,
    "agent_session.redact_secrets": _agent,
}


@pytest.fixture(autouse=True)
def _no_client_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("entry", sorted(TEXT_ENTRY_POINTS))
@pytest.mark.parametrize("case", LEAKS, ids=[case.id for case in LEAKS])
def test_text_entry_points_remove_every_secret(case: Leak, entry: str) -> None:
    redacted = TEXT_ENTRY_POINTS[entry](case.text)

    _assert_redacted(case, redacted)


@pytest.mark.parametrize("case", LEAKS, ids=[case.id for case in LEAKS])
def test_lt_run_argv_removes_every_secret(case: Leak) -> None:
    argv = case.argv if case.argv is not None else tuple(shlex.split(case.text))

    _assert_redacted(case, _argv(argv))


def _assert_redacted(case: Leak, redacted: str) -> None:
    assert REDACTED in redacted
    for secret in case.secrets:
        assert secret not in redacted, redacted
    for kept in case.keep:
        assert kept in redacted, redacted
    for bad in case.forbid:
        assert bad not in redacted, redacted


@pytest.mark.parametrize("entry", sorted(TEXT_ENTRY_POINTS))
@pytest.mark.parametrize("text", SAFE)
def test_text_entry_points_leave_ordinary_text_alone(text: str, entry: str) -> None:
    assert TEXT_ENTRY_POINTS[entry](text) == text


@pytest.mark.parametrize("text", SAFE)
def test_lt_run_argv_leaves_ordinary_commands_alone(text: str) -> None:
    argv = shlex.split(text)

    assert redact_argv(argv) == argv


def test_the_clients_own_credential_values_are_removed_everywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAB_TRACKER_ACCESS_TOKEN", "lt-own-credential-42")
    text = "echo lt-own-credential-42 and lt-own-credential-42%21 done"

    outputs = [entry(text) for entry in TEXT_ENTRY_POINTS.values()]
    outputs.append(_argv(tuple(shlex.split(text))))

    for redacted in outputs:
        assert "lt-own-credential-42" not in redacted
        assert "done" in redacted


def test_extra_literal_secrets_are_removed_including_url_encoded_forms() -> None:
    redacted = redact_capture_text("a p@ss/w0rd b p%40ss%2Fw0rd c", secrets=("p@ss/w0rd",))

    assert "p@ss" not in redacted and "p%40ss" not in redacted
    assert redacted.startswith("a ") and redacted.endswith(" c")


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("password", True),
        ("DB_PASSWORD", True),
        ("PGPASSWORD", True),
        ("api-key", True),
        ("apiKey", True),
        ("accessToken", True),
        ("client_secret", True),
        ("x-amz-credential", True),
        ("auth", True),
        ("max_tokens", False),
        ("max-tokens", False),
        ("tokenizer", False),
        ("token_count", False),
        ("num-pass", False),
        ("password-file", False),
        ("no-password", False),
        ("token-name", False),
        ("ssh-key", False),
        ("author", False),
        ("", False),
    ],
)
def test_a_name_is_secret_only_when_it_ends_in_a_secret_word(name: str, secret: bool) -> None:
    assert looks_secret_name(name) is secret
