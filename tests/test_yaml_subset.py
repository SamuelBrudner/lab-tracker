from __future__ import annotations

import pytest

from lab_tracker_client.yaml_subset import YamlSubsetError, load_yaml, parse_yaml_subset

DVC_LOCK = """\
schema: '2.0'
stages:
  prepare:
    cmd: python src/prepare.py data/data.xml
    deps:
    - path: data/data.xml
      hash: md5
      md5: 22a1a2931c8370d3aeedd7183606fd7f
      size: 14445097
    params:
      params.yaml:
        prepare.seed: 20170428
        prepare.split: 0.2
    outs:
    - path: data/prepared
      md5: 153aad06d376b6595932470e459ef42a.dir
      size: 8437363
      nfiles: 2
  train@1:
    cmd:
    - python a.py
    - "python b.py --flag: yes"
    frozen: true
"""


def test_parses_the_dvc_lock_subset() -> None:
    parsed = parse_yaml_subset(DVC_LOCK)

    assert parsed["schema"] == "2.0"
    prepare = parsed["stages"]["prepare"]
    assert prepare["cmd"] == "python src/prepare.py data/data.xml"
    assert prepare["deps"] == [
        {
            "path": "data/data.xml",
            "hash": "md5",
            "md5": "22a1a2931c8370d3aeedd7183606fd7f",
            "size": 14445097,
        }
    ]
    assert prepare["params"] == {"params.yaml": {"prepare.seed": 20170428, "prepare.split": 0.2}}
    assert prepare["outs"][0]["md5"].endswith(".dir")
    assert parsed["stages"]["train@1"]["cmd"] == ["python a.py", "python b.py --flag: yes"]
    assert parsed["stages"]["train@1"]["frozen"] is True


def test_block_scalars_comments_and_plain_continuations() -> None:
    parsed = parse_yaml_subset(
        """\
# leading comment
name: Lab Tracker # trailing comment
runs:
  using: composite
  steps:
    - name: Report
      shell: bash
      env:
        TOKEN: ${{ inputs.access-token }}
      run: |
        set -eu
        echo "hash # not a comment"

        lt repo report --fail-silent
    - run: >-
        folded
        text
long: first part
  second part
empty:
flow: [a, 'b c', {k: v}]
quoted: "tab\\there # not a comment"
single: 'it''s'
"""
    )

    step = parsed["runs"]["steps"][0]
    assert parsed["name"] == "Lab Tracker"
    assert step["env"] == {"TOKEN": "${{ inputs.access-token }}"}
    assert step["run"] == ('set -eu\necho "hash # not a comment"\n\nlt repo report --fail-silent\n')
    assert parsed["runs"]["steps"][1]["run"] == "folded text"
    assert parsed["long"] == "first part second part"
    assert parsed["empty"] is None
    assert parsed["flow"] == ["a", "b c", {"k": "v"}]
    assert parsed["quoted"] == "tab\there # not a comment"
    assert parsed["single"] == "it's"


def test_multi_line_double_quoted_scalars_fold_like_yaml() -> None:
    parsed = parse_yaml_subset(
        'escaped: "abc \\\n    def"\n'
        'joined: "abc\\\n    def"\n'
        'folded: "abc   \n    def"\n'
        'paragraph: "abc\n\n    def"\n'
    )

    assert parsed == {
        "escaped": "abc def",
        "joined": "abcdef",
        "folded": "abc def",
        "paragraph": "abc\ndef",
    }


def test_rejects_constructs_outside_the_subset() -> None:
    with pytest.raises(YamlSubsetError):
        parse_yaml_subset("a: &anchor 1\nb: *anchor\n")
    with pytest.raises(YamlSubsetError):
        parse_yaml_subset("a:\n\tb: 1\n")
    with pytest.raises(YamlSubsetError):
        parse_yaml_subset("key: value\n  other: 1\n")


def test_load_yaml_prefers_pyyaml_when_installed(monkeypatch) -> None:
    import sys
    import types

    fake = types.ModuleType("yaml")
    fake.safe_load = lambda text: {"from": "pyyaml", "text": text}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yaml", fake)

    assert load_yaml("a: 1\n") == {"from": "pyyaml", "text": "a: 1\n"}


def test_load_yaml_falls_back_to_the_subset_parser(monkeypatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "yaml", None)

    assert load_yaml("a: 1\nb: [x]\n") == {"a": 1, "b": ["x"]}
