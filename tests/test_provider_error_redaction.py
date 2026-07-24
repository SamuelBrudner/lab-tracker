from __future__ import annotations

import json
import logging
import sys
from urllib.parse import quote

from lab_tracker.logging import JsonFormatter
from lab_tracker.provider_error_redaction import provider_error_message


def test_provider_error_message_redacts_raw_and_encoded_credentials() -> None:
    api_key = "nonstandard/google secret"
    encoded_key = quote(api_key, safe="")
    message = (
        f"quota exceeded for https://provider.test/v1?key={api_key}&retry=true; "
        f"https%3A%2F%2Fprovider.test%2Fv1%3Fkey%3D{encoded_key}; "
        f"x-goog-api-key: {api_key}"
    )

    redacted = provider_error_message(message, secrets=(api_key,))

    assert "quota exceeded" in redacted
    assert "retry=true" in redacted
    assert api_key not in redacted
    assert encoded_key not in redacted
    assert "?key=" not in redacted
    assert "%3Fkey%3D" not in redacted
    assert "[REDACTED]" in redacted


def test_json_formatter_redacts_provider_credentials_from_message_and_traceback() -> None:
    api_key = "AIza" + ("0" * 35)
    encoded_key = quote(api_key, safe="")
    unsafe_detail = (
        f"provider failed at https://provider.test/v1?key={api_key}; "
        f"https%3A%2F%2Fprovider.test%2Fv1%3Fkey%3D{encoded_key}"
    )
    record = logging.LogRecord(
        name="lab_tracker.provider",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=unsafe_detail,
        args=(),
        exc_info=None,
    )
    try:
        raise RuntimeError(unsafe_detail)
    except RuntimeError:
        record.exc_info = sys.exc_info()

    payload = json.loads(JsonFormatter().format(record))
    rendered = json.dumps(payload)

    assert payload["message"].startswith("provider failed")
    assert "RuntimeError" in payload["exception"]
    assert api_key not in rendered
    assert encoded_key not in rendered
    assert "?key=" not in rendered
    assert "%3Fkey%3D" not in rendered
