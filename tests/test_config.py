import os

import pytest

from cc_slack.config import ConfigError, resolve_cwd


def test_resolve_cwd_inside_root(tmp_path):
    (tmp_path / "proj").mkdir()
    real = os.path.realpath(str(tmp_path))
    assert resolve_cwd(str(tmp_path / "proj"), (real,)) == os.path.join(real, "proj")
    assert resolve_cwd(str(tmp_path), (real,)) == real


def test_resolve_cwd_rejects_outside_and_missing(tmp_path):
    real = os.path.realpath(str(tmp_path / "root"))
    (tmp_path / "root").mkdir()
    (tmp_path / "rootx").mkdir()
    with pytest.raises(ConfigError):
        resolve_cwd(str(tmp_path / "rootx"), (real,))  # prefix trick must not pass
    with pytest.raises(ConfigError):
        resolve_cwd(str(tmp_path / "root" / "nope"), (real,))


def test_load_dotenv_strips_inline_comments(tmp_path, monkeypatch):
    from cc_slack.config import load_dotenv
    f = tmp_path / ".env"
    f.write_text('A=3          # comment\nB="x # not a comment"\n# C=9\nD=hello\n')
    for k in "ABCD":
        monkeypatch.delenv(k, raising=False)
    load_dotenv(f)
    assert os.environ["A"] == "3" and os.environ["B"] == "x # not a comment" and os.environ["D"] == "hello"
    assert "C" not in os.environ
