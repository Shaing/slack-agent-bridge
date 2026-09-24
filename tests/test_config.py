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


def test_find_env_file_override_then_xdg_then_cwd(tmp_path, monkeypatch):
    from pathlib import Path

    from cc_slack.config import find_env_file
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("CC_ENV_FILE", raising=False)
    assert find_env_file() == Path(".env")  # nothing else exists yet
    xdg = tmp_path / "xdg" / "cc-slack" / ".env"
    xdg.parent.mkdir(parents=True); xdg.write_text("")
    assert find_env_file() == xdg  # preferred over ./.env
    monkeypatch.setenv("CC_ENV_FILE", str(tmp_path / "custom.env"))
    with pytest.raises(ConfigError):
        find_env_file()  # explicit override must exist
    (tmp_path / "custom.env").write_text("")
    assert find_env_file() == tmp_path / "custom.env"
