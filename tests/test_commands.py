from cc_slack.slack_app import normalize_mode, parse_command, parse_prefixes


def test_parse_prefixes():
    assert parse_prefixes("cwd:/home/x/proj\nlist files") == ({"cwd": "/home/x/proj"}, "list files")
    assert parse_prefixes("CWD: ~/proj  do it") == ({"cwd": "~/proj"}, "do it")
    assert parse_prefixes("cwd:/only") == ({"cwd": "/only"}, "")
    assert parse_prefixes("no prefix here") == ({}, "no prefix here")
    assert parse_prefixes("mode:plan model:opus cwd:/a refactor it") == (
        {"mode": "plan", "model": "opus", "cwd": "/a"},
        "refactor it",
    )
    assert parse_prefixes("model:claude-opus-5-5[1m]\nhi") == ({"model": "claude-opus-5-5[1m]"}, "hi")
    # only leading options are parsed
    assert parse_prefixes("fix mode:plan bug") == ({}, "fix mode:plan bug")


def test_normalize_mode():
    assert normalize_mode("acceptedits") == "acceptEdits"
    assert normalize_mode(" PLAN ") == "plan"
    assert normalize_mode("weird") == "weird"


def test_parse_command():
    assert parse_command("!stop") == ("stop", "")
    assert parse_command("!cwd /a/b") == ("cwd", "/a/b")
    assert parse_command("!Mode  plan") == ("mode", "plan")
    assert parse_command("hello") is None
