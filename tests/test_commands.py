from cc_slack.slack_app import parse_command, parse_cwd_prefix


def test_parse_cwd_prefix():
    assert parse_cwd_prefix("cwd:/home/x/proj\nlist files") == ("/home/x/proj", "list files")
    assert parse_cwd_prefix("CWD: ~/proj  do it") == ("~/proj", "do it")
    assert parse_cwd_prefix("cwd:/only") == ("/only", "")
    assert parse_cwd_prefix("no prefix here") == (None, "no prefix here")


def test_parse_command():
    assert parse_command("!stop") == ("stop", "")
    assert parse_command("!cwd /a/b") == ("cwd", "/a/b")
    assert parse_command("!Mode  plan") == ("mode", "plan")
    assert parse_command("hello") is None
