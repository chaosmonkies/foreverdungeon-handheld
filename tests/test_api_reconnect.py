import json

from conftest import pair, pump
from foreverdungeon_handheld.app import S


def _select(app, command):
    idx = [c["command"] for c in app.choices()].index(command)
    app.cursor = idx
    app.handle_key("enter")


def test_envelope_persisted_before_send(make_app, server, clock):
    app = pair(make_app(), server, clock)
    server.world.faults["action"].append(503)
    _select(app, "take copper coin")
    # persisted synchronously on the UI thread, before the worker sends anything
    env = json.loads(app.cache.pending_path.read_text())
    assert env["command"] == "take copper coin" and env["expected_revision"] == 3
    assert server.world.action_posts == 0


def test_drop_after_commit_resends_same_key_no_duplicate(make_app, server, clock):
    app = pair(make_app(), server, clock)
    server.world.faults["action"].append("drop_after_commit")
    _select(app, "take copper coin")
    key = app.pending["idempotency_key"]
    pump(app, 1)
    assert app.state == S.ACTION_PENDING and app.pending["idempotency_key"] == key
    assert json.loads(app.cache.pending_path.read_text())["idempotency_key"] == key
    assert server.world.gold == 11 and len(server.world.events) == 1  # committed once
    assert "PENDING" in app.screen().status
    # user cannot queue a second mechanical action
    app.handle_key("enter")
    assert app.pending["idempotency_key"] == key
    clock.advance(10)
    pump(app, 3)
    assert app.pending is None and not app.cache.pending_path.exists()
    assert server.world.gold == 11 and len(server.world.events) == 1  # replay, no duplicate
    assert app.snapshot["last_narration"] == "You pocket the copper coin."
    assert app.snapshot["state"]["character"]["gold"] == 11


def test_restart_shows_cache_immediately_and_resends(make_app, make_cache, server, clock):
    cache = make_cache()
    app = pair(make_app(cache), server, clock)
    server.world.faults["action"].append("drop_after_commit")
    _select(app, "take copper coin")
    pump(app, 1)
    key = app.pending["idempotency_key"]
    app.stop()
    # restart with the same files
    app2 = make_app(make_cache())
    app2.start()
    assert app2.state == S.ACTION_PENDING
    assert app2.pending["idempotency_key"] == key
    texts = " ".join(r.text for r in app2.screen().rows)
    assert "Rain marks the tavern" in texts  # cached text before any network
    pump(app2, 3)
    assert app2.pending is None and server.world.gold == 11 and len(server.world.events) == 1


def test_409_conflict_requires_new_choice(make_app, server, clock):
    app = pair(make_app(), server, clock)
    server.world.revision = 9  # world changed elsewhere (e.g. the website)
    _select(app, "take copper coin")
    pump(app, 3)
    assert app.pending is None and not app.cache.pending_path.exists()
    assert app.state == S.PLAY
    assert "world changed" in app.message.lower()
    assert server.world.gold == 10  # not applied
    assert app.snapshot["state"]["character"]["revision"] == 9


def test_401_revoke_keeps_offline_cache(make_app, server, clock):
    app = pair(make_app(), server, clock)
    state_before = app.cache.state_path.read_text()
    for t in list(server.world.tokens):
        server.world.revoked.add(t)
    app.handle_key("r")
    pump(app, 2)
    assert app.state == S.REVOKED
    assert not app.cache.token_path.exists()
    assert app.cache.state_path.read_text() == state_before
    # the cached world is still readable
    app.handle_key("w")
    assert app.state == S.MENU_WORLD and "The Lantern Tavern" in app.screen().rows[0].text
    app.handle_key("esc")
    assert app.state == S.REVOKED


def test_429_503_malformed_keep_same_envelope(make_app, server, clock):
    app = pair(make_app(), server, clock)
    server.world.faults["action"].extend([429, 503, "malformed"])
    _select(app, "look")
    key = app.pending["idempotency_key"]
    for _ in range(3):
        pump(app, 2)
        assert app.pending and app.pending["idempotency_key"] == key
        clock.advance(70)
    pump(app, 3)
    assert app.pending is None
    assert len(server.world.events) == 1


def test_state_errors_go_offline_without_clearing(make_app, server, clock):
    app = pair(make_app(), server, clock)
    server.world.faults["state"].extend(["malformed", 503, 429])
    for _ in range(3):
        app.next_state_at = 0
        pump(app, 2)
        assert app.offline and app.state == S.PLAY
        assert "OFFLINE" in app.screen().status
        assert app.snapshot["state"]["location"]["name"] == "The Lantern Tavern"
    clock.advance(120)
    pump(app, 2)
    assert not app.offline


def test_network_down_marks_offline(make_app, server, clock, make_cache):
    app = pair(make_app(), server, clock)
    app.api.base_url = "http://127.0.0.1:9"  # nothing listens
    _select(app, "look")
    pump(app, 1)
    assert app.offline and app.pending and app.cache.pending_path.exists()


def test_text_entry_submits_freeform(make_app, server, clock):
    app = pair(make_app(), server, clock)
    app.handle_key("t")
    assert app.state == S.COMMAND_ENTRY
    for ch in "hum a song":
        app.handle_key(ch)
    app.handle_key("back")
    app.handle_key("g")
    assert app.screen().input_line == "hum a song"
    app.handle_key("enter")
    pump(app, 3)
    assert server.world.events[-1]["summary"].startswith("You hum a song")
