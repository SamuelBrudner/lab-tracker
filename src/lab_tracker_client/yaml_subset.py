"""A small, dependency-free reader for machine-written YAML files.

PyYAML is not a Lab Tracker dependency (not even a transitive one), yet two
capture paths read YAML: ``lt pipeline dvc`` reads ``dvc.lock`` and the tests
read the GitHub Action metadata. :func:`load_yaml` uses PyYAML's ``safe_load``
when it happens to be installed and otherwise falls back to
:func:`parse_yaml_subset`, which reads the block-style subset those files use:

* block mappings and block sequences, including the compact ``key:\\n- item``
  form and mapping items that start on the dash line (``- path: x``);
* plain scalars (with multi-line continuations), single- and double-quoted
  scalars, literal (``|``) and folded (``>``) block scalars with chomping
  indicators, and simple flow collections (``[a, b]``, ``{k: v}``);
* comments and ``---`` document markers.

Plain scalars resolve with the YAML 1.2 core schema (null, booleans, integers,
floats); mapping keys are always strings. Anchors, aliases, tags, complex keys
and tab indentation raise :class:`YamlSubsetError` instead of being guessed.
"""

from __future__ import annotations

import importlib
import re
from typing import Any

__all__ = ["YamlSubsetError", "load_yaml", "parse_yaml_subset"]

_KEY_PATTERN = re.compile(r"^(?P<key>[^#'\"\[\]{},\s][^#]*?)\s*:(?:\s+(?P<rest>.*)|$)")
_INT_PATTERN = re.compile(r"^[-+]?[0-9]+$")
_FLOAT_PATTERN = re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$")
_NULLS = frozenset({"", "~", "null", "Null", "NULL"})
_TRUES = frozenset({"true", "True", "TRUE"})
_FALSES = frozenset({"false", "False", "FALSE"})
_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
}
_HEX_ESCAPE_LENGTHS = {"x": 2, "u": 4, "U": 8}


class YamlSubsetError(ValueError):
    """The text is not YAML, or uses a construct outside the supported subset."""


class _Unterminated(Exception):
    """A quoted scalar or flow collection continues on the next line."""


def load_yaml(text: str) -> Any:
    """Parse ``text`` with PyYAML when it is installed, else with the subset parser."""

    try:
        module = importlib.import_module("yaml")
    except ImportError:
        return parse_yaml_subset(text)
    try:
        return module.safe_load(text)
    except Exception as exc:  # noqa: BLE001 - PyYAML errors surface as ours.
        raise YamlSubsetError(str(exc)) from exc


def parse_yaml_subset(text: str) -> Any:
    """Parse the block-style YAML subset described in the module docstring."""

    parser = _Parser(text)
    value = parser.parse_block(-1)
    if parser.peek() is not None:
        raise parser.error("unexpected content after the document")
    return value


class _Parser:
    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()
        self.pos = 0

    def error(self, message: str) -> YamlSubsetError:
        return YamlSubsetError(f"line {self.pos + 1}: {message}")

    def peek(self) -> tuple[int, str] | None:
        """Skip blank/comment lines; return the next line's (indent, content)."""

        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            content = line.lstrip(" ")
            leading = line[: len(line) - len(content)]
            stripped = content.strip()
            if not stripped or stripped.startswith("#") or stripped in {"---", "..."}:
                self.pos += 1
                continue
            if content.startswith("\t") or "\t" in leading:
                raise self.error("tabs are not allowed in indentation")
            return len(leading), content.rstrip()
        return None

    def parse_block(self, parent_indent: int) -> Any:
        item = self.peek()
        if item is None or item[0] <= parent_indent:
            return None
        indent, content = item
        if _is_sequence_item(content):
            return self.parse_sequence(indent)
        if _split_key(content) is not None:
            return self.parse_mapping(indent)
        self.pos += 1
        return self.parse_value(content, parent_indent, compact_sequence=False)

    def parse_mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while True:
            item = self.peek()
            if item is None or item[0] < indent:
                return result
            line_indent, content = item
            if line_indent > indent:
                raise self.error("unexpected indentation")
            split = _split_key(content)
            if split is None:
                if _is_sequence_item(content):
                    raise self.error("a sequence item cannot follow a mapping entry")
                raise self.error(f"expected a 'key: value' entry, got {content!r}")
            key, rest = split
            self.pos += 1
            result[key] = self.parse_value(rest, indent, compact_sequence=True)

    def parse_sequence(self, indent: int) -> list[Any]:
        result: list[Any] = []
        while True:
            item = self.peek()
            if item is None or item[0] < indent:
                return result
            line_indent, content = item
            if line_indent > indent:
                raise self.error("unexpected indentation")
            if not _is_sequence_item(content):
                return result
            body = content[1:]
            stripped = body.lstrip(" ")
            if not stripped or stripped.startswith("#"):
                self.pos += 1
                result.append(self.parse_value("", indent, compact_sequence=False))
                continue
            column = indent + 1 + len(body) - len(stripped)
            if _is_sequence_item(stripped) or _split_key(stripped) is not None:
                # The item is itself a mapping or sequence starting on the dash
                # line: re-read that line with its content at its own column.
                self.lines[self.pos] = " " * column + stripped
                result.append(self.parse_block(column - 1))
                continue
            self.pos += 1
            result.append(self.parse_value(stripped, indent, compact_sequence=False))

    def parse_value(self, rest: str, indent: int, *, compact_sequence: bool) -> Any:
        text = rest.strip()
        if not text or text.startswith("#"):
            item = self.peek()
            if item is None:
                return None
            next_indent, next_content = item
            if next_indent > indent:
                return self.parse_block(indent)
            if next_indent == indent and compact_sequence and _is_sequence_item(next_content):
                return self.parse_sequence(indent)
            return None
        first = text[0]
        if first in "&*!?":
            raise self.error("anchors, aliases, tags and complex keys are not supported")
        if first in "|>":
            return self.parse_block_scalar(text, indent)
        if first in "\"'[{":
            return self.parse_inline(text)
        return self.parse_plain(text, indent)

    def parse_inline(self, text: str) -> Any:
        buffer = text
        while True:
            try:
                value, remainder = _flow_value(buffer, 0, top_level=True)
            except _Unterminated:
                if self.pos >= len(self.lines):
                    raise self.error("unterminated quoted scalar or flow collection") from None
                buffer = _fold_continuation(buffer, self.lines[self.pos])
                self.pos += 1
                continue
            leftover = remainder.strip()
            if leftover and not leftover.startswith("#"):
                raise self.error(f"unexpected text after a value: {leftover!r}")
            return value

    def parse_plain(self, text: str, indent: int) -> Any:
        parts = [_strip_comment(text)]
        while True:
            item = self.peek()
            if item is None or item[0] <= indent:
                break
            _next_indent, content = item
            if _split_key(content) is not None:
                raise self.error("mapping values are not allowed inside a plain scalar")
            parts.append(_strip_comment(content))
            self.pos += 1
        return _resolve_plain(" ".join(part for part in parts if part))

    def parse_block_scalar(self, header: str, indent: int) -> str:
        indicator = _strip_comment(header)
        style = indicator[0]
        chomp = "clip"
        explicit: int | None = None
        for char in indicator[1:]:
            if char == "-":
                chomp = "strip"
            elif char == "+":
                chomp = "keep"
            elif char.isdigit() and char != "0":
                explicit = int(char)
            else:
                raise self.error(f"bad block scalar header {indicator!r}")
        block_indent = None if explicit is None else max(indent, 0) + explicit
        lines: list[str] = []
        while self.pos < len(self.lines):
            raw = self.lines[self.pos]
            if not raw.strip():
                lines.append("")
                self.pos += 1
                continue
            line_indent = len(raw) - len(raw.lstrip(" "))
            if block_indent is None:
                if line_indent <= indent:
                    break
                block_indent = line_indent
            if line_indent < block_indent:
                break
            lines.append(raw[block_indent:])
            self.pos += 1
        trailing = len(lines) - len(_rstrip_blank(lines))
        content = lines[: len(lines) - trailing]
        body = "\n".join(content) if style == "|" else _fold(content)
        if chomp == "strip" or not content:
            return body if chomp != "keep" else "\n" * trailing
        if chomp == "keep":
            return body + "\n" * (trailing + 1)
        return body + "\n"


def _fold_continuation(buffer: str, line: str) -> str:
    """Append the next line of a multi-line quoted scalar or flow collection.

    YAML folds the line break into one space (trailing and leading white space
    dropped), keeps an empty line as a line feed, and in a double-quoted scalar
    joins the lines with nothing when the break is escaped with a backslash.
    """

    continuation = line.strip()
    if not continuation:
        return buffer + "\n"
    if buffer.endswith("\n"):
        return buffer + continuation
    backslashes = len(buffer) - len(buffer.rstrip("\\"))
    if buffer.startswith('"') and backslashes % 2 == 1:
        return buffer[:-1] + continuation
    return buffer.rstrip(" \t") + " " + continuation


def _rstrip_blank(lines: list[str]) -> list[str]:
    end = len(lines)
    while end and not lines[end - 1]:
        end -= 1
    return lines[:end]


def _fold(lines: list[str]) -> str:
    text = ""
    blank_run = 0
    previous_more_indented = False
    for line in lines:
        if not line:
            blank_run += 1
            continue
        more_indented = line.startswith((" ", "\t"))
        if text:
            if blank_run:
                extra = 1 if (more_indented or previous_more_indented) else 0
                text += "\n" * (blank_run + extra)
            elif more_indented or previous_more_indented:
                text += "\n"
            else:
                text += " "
        else:
            text += "\n" * blank_run
        text += line
        blank_run = 0
        previous_more_indented = more_indented
    return text


def _is_sequence_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _split_key(content: str) -> tuple[str, str] | None:
    if content[:1] in {'"', "'"}:
        try:
            key, remainder = _quoted(content, 0)
        except _Unterminated:
            return None
        remainder = remainder.lstrip(" ")
        if remainder == ":" or remainder.startswith(": "):
            return key, remainder[1:].strip()
        return None
    if _is_sequence_item(content):
        return None
    match = _KEY_PATTERN.match(content)
    if match is None:
        return None
    return match.group("key").strip(), (match.group("rest") or "").strip()


def _strip_comment(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("#"):
        return ""
    match = re.search(r"\s#", stripped)
    return stripped[: match.start()].rstrip() if match else stripped


def _resolve_plain(text: str) -> Any:
    if text in _NULLS:
        return None
    if text in _TRUES:
        return True
    if text in _FALSES:
        return False
    if _INT_PATTERN.match(text):
        return int(text)
    if re.fullmatch(r"0o[0-7]+", text):
        return int(text[2:], 8)
    if re.fullmatch(r"0x[0-9a-fA-F]+", text):
        return int(text[2:], 16)
    if _FLOAT_PATTERN.match(text):
        return float(text)
    if re.fullmatch(r"[-+]?\.(?:inf|Inf|INF)", text):
        return float("-inf") if text.startswith("-") else float("inf")
    if re.fullmatch(r"\.(?:nan|NaN|NAN)", text):
        return float("nan")
    return text


def _quoted(text: str, start: int) -> tuple[str, str]:
    quote = text[start]
    index = start + 1
    out: list[str] = []
    while index < len(text):
        char = text[index]
        if quote == "'":
            if char == "'":
                if text[index + 1 : index + 2] == "'":
                    out.append("'")
                    index += 2
                    continue
                return "".join(out), text[index + 1 :]
            out.append(char)
            index += 1
            continue
        if char == '"':
            return "".join(out), text[index + 1 :]
        if char == "\\":
            escape = text[index + 1 : index + 2]
            if not escape:
                raise _Unterminated
            if escape in _HEX_ESCAPE_LENGTHS:
                length = _HEX_ESCAPE_LENGTHS[escape]
                digits = text[index + 2 : index + 2 + length]
                if len(digits) != length:
                    raise YamlSubsetError(f"bad \\{escape} escape in {text!r}")
                out.append(chr(int(digits, 16)))
                index += 2 + length
                continue
            if escape not in _ESCAPES:
                raise YamlSubsetError(f"unknown escape \\{escape} in {text!r}")
            out.append(_ESCAPES[escape])
            index += 2
            continue
        out.append(char)
        index += 1
    raise _Unterminated


def _flow_value(text: str, index: int, *, top_level: bool = False) -> tuple[Any, str]:
    """Parse one flow value at ``index``; return it and the unparsed remainder."""

    while index < len(text) and text[index] == " ":
        index += 1
    if index >= len(text):
        raise _Unterminated
    char = text[index]
    if char in {'"', "'"}:
        return _quoted(text, index)
    if char == "[":
        return _flow_sequence(text, index + 1)
    if char == "{":
        return _flow_mapping(text, index + 1)
    if top_level:
        return _resolve_plain(_strip_comment(text[index:])), ""
    end = index
    while end < len(text) and text[end] not in ",]}" and not text.startswith(": ", end):
        end += 1
    return _resolve_plain(text[index:end].strip()), text[end:]


def _flow_sequence(text: str, index: int) -> tuple[list[Any], str]:
    items: list[Any] = []
    remainder = text[index:]
    while True:
        remainder = remainder.lstrip(" ")
        if not remainder:
            raise _Unterminated
        if remainder[0] == "]":
            return items, remainder[1:]
        value, remainder = _flow_value(remainder, 0)
        items.append(value)
        remainder = remainder.lstrip(" ")
        if remainder.startswith(","):
            remainder = remainder[1:]
        elif not remainder:
            raise _Unterminated
        elif remainder[0] != "]":
            raise YamlSubsetError(f"expected ',' or ']' in flow sequence {text!r}")


def _flow_mapping(text: str, index: int) -> tuple[dict[str, Any], str]:
    result: dict[str, Any] = {}
    remainder = text[index:]
    while True:
        remainder = remainder.lstrip(" ")
        if not remainder:
            raise _Unterminated
        if remainder[0] == "}":
            return result, remainder[1:]
        key, remainder = _flow_value(remainder, 0)
        remainder = remainder.lstrip(" ")
        if not remainder.startswith(":"):
            raise YamlSubsetError(f"expected ':' in flow mapping {text!r}")
        value, remainder = _flow_value(remainder[1:], 0)
        result[str(key)] = value
        remainder = remainder.lstrip(" ")
        if remainder.startswith(","):
            remainder = remainder[1:]
        elif not remainder:
            raise _Unterminated
        elif remainder[0] != "}":
            raise YamlSubsetError(f"expected ',' or '}}' in flow mapping {text!r}")
