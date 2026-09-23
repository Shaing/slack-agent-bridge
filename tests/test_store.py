from cc_slack.store import SessionRegistry, ThreadStore


def test_roundtrip(tmp_path):
    store = ThreadStore(tmp_path / "s" / "threads.json")
    reg = SessionRegistry(store)
    s = reg.create("C1", "1.2", "/tmp", "U1")
    s.record.session_id = "abc"
    s.record.in_flight = {"status_ts": "9.9"}
    reg.persist()

    reg2 = SessionRegistry(ThreadStore(store.path))
    got = reg2.get("C1:1.2")
    assert got is not None and got.record.session_id == "abc" and got.record.cwd == "/tmp"
    assert [r.thread_key for r in reg2.in_flight()] == ["C1:1.2"]
    assert reg2.get("nope") is None


def test_unknown_fields_ignored(tmp_path):
    p = tmp_path / "t.json"
    p.write_text('{"C:1": {"thread_key": "C:1", "channel": "C", "thread_ts": "1", "cwd": "/", "owner": "U", "zzz": 1}}')
    assert SessionRegistry(ThreadStore(p)).get("C:1").record.owner == "U"
