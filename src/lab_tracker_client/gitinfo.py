"""Shared git helpers for the client capture adapters.

Every adapter that records git provenance (``lt repo``, ``lt hpc``, ``lt git
snapshot`` and figure ``run_context``) goes through this module, so that:

* a remote URL is always passed through :func:`sanitize_remote_url` before it
  is stored in an outbox event, rendered into a note body or copied into note
  metadata, because git remotes routinely embed credentials.
"""

from __future__ import annotations

import re

# ``scheme://authority rest`` — authority is everything up to the first
# '/', '?' or '#', so a raw '@' inside a password stays in the authority.
_SCHEME_URL = re.compile(
    r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<authority>[^/?#]*)(?P<rest>.*)\Z",
    re.DOTALL,
)
# Transports whose login name is addressing (``ssh://git@host``), not a secret.
_SSH_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git"})


def sanitize_remote_url(remote: str) -> str:
    """Return ``remote`` with every credential-bearing part removed.

    * ``http(s)://`` and every other non-ssh scheme: all userinfo is dropped —
      a bare ``https://<token>@host`` username is indistinguishable from a
      personal access token, so it is never kept.
    * ``ssh://`` (and ``git+ssh://``/``ssh+git://``): the login name is kept
      (``ssh://git@host/...`` is addressing); any password is dropped.
    * Query strings and fragments are dropped from scheme URLs; git never needs
      them and they are a common place for ``access_token=`` style secrets.
    * scp-like ``[user@]host:path`` addressing (``git@github.com:lab/repo``) and
      local paths are returned unchanged: git's scp-like syntax has no password
      or query component.

    Credential-free remotes are returned unchanged (apart from surrounding
    whitespace), so values derived from them — such as
    :func:`lab_tracker_client.repo.normalize_remote` identities — stay stable.
    """

    cleaned = remote.strip()
    if not cleaned:
        return ""
    match = _SCHEME_URL.match(cleaned)
    if match is None:
        return cleaned
    scheme = match["scheme"]
    authority = match["authority"]
    userinfo, at, host = authority.rpartition("@")
    if at:
        login = userinfo.partition(":")[0]
        keep_login = scheme.lower() in _SSH_SCHEMES and bool(login)
        authority = f"{login}@{host}" if keep_login else host
    path = re.split(r"[?#]", match["rest"], maxsplit=1)[0]
    return f"{scheme}://{authority}{path}"
