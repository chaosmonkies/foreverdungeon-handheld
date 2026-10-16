import os
import stat

from foreverdungeon_handheld.cache import Cache, atomic_write


def test_atomic_write_mode(tmp_path):
    p = tmp_path / "a" / "f"
    atomic_write(p, b"x")
    assert p.read_bytes() == b"x" and stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert [f.name for f in p.parent.iterdir()] == ["f"]  # no temp leftovers


def test_roundtrip(tmp_path):
    c = Cache(tmp_path / "s.json", tmp_path / "tok", max_recent_events=3)
    c.save_token("t", "chr_1")
    assert c.load_token() == {"access_token": "t", "character_id": "chr_1"}
    c.save_state({"recent": list(range(10))})
    assert c.load_state()["recent"] == [7, 8, 9]
    env = {"character_id": "chr_1", "command": "look", "idempotency_key": "k" * 36}
    c.save_pending(env)
    assert c.load_pending() == env
    c.clear_pending()
    assert c.load_pending() is None


def test_corrupt_files_recovered(tmp_path):
    c = Cache(tmp_path / "s.json", tmp_path / "tok")
    (tmp_path / "s.json").write_bytes(b"\x00\xffgarbage{")
    (tmp_path / "tok").write_text("[1,2]")
    (tmp_path / "pending-action.json").write_text('{"command":')
    assert c.load_state() is None and c.load_token() is None and c.load_pending() is None
    assert len(c.recovered_corrupt) == 3
    assert any(f.name.startswith("s.json.corrupt-") for f in tmp_path.iterdir())
    c.save_state({"ok": 1})
    assert c.load_state() == {"ok": 1}


def test_app_starts_after_corrupt_cache(make_app, make_cache, server, clock):
    from conftest import pair, pump
    app = pair(make_app(make_cache("x")), server, clock)
    app.cache.state_path.write_text("{{{")
    app2 = make_app(make_cache("x"))
    app2.start()
    assert "corrupt" in app2.message
    pump(app2, 2)
    assert app2.snapshot["state"]["location"]["name"] == "The Lantern Tavern"
