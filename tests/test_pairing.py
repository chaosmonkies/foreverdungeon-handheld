import json
import os
import stat
import time

from conftest import pair, pump
from foreverdungeon_handheld.app import S


def test_pending_then_approved_saves_token_0600(make_app, server, clock):
    app = make_app()
    app.start()
    pump(app, 1)
    assert app.state == S.SHOW_CODE
    scr = app.screen()
    assert any(r.kind == "big" and r.text.startswith("ABCDE-") for r in scr.rows)
    assert all(app.pairing["device_code"] not in r.text for r in scr.rows)  # never displayed
    # 202 pending
    clock.advance(1.5)
    pump(app, 2)
    assert app.state == S.SHOW_CODE
    server.world.approve(app.pairing["user_code"])
    clock.advance(1.5)
    pump(app, 3)
    assert app.state == S.PLAY
    assert app.pairing is None  # device code erased
    tok = app.cache.token_path
    assert stat.S_IMODE(os.stat(tok).st_mode) == 0o600
    data = json.loads(tok.read_text())
    assert data["character_id"] == "chr_test" and "device_code" not in data
    assert server.world.last_auth.startswith("Bearer ")


def test_poll_respects_interval(make_app, server, clock):
    app = make_app()
    app.start()
    pump(app, 1)
    first = app.next_poll_at
    pump(app, 5)  # clock not advanced: must not poll
    assert app.next_poll_at == first and "poll" not in app.inflight


def test_expired_410_then_new_code(make_app, server, clock):
    app = make_app()
    app.start()
    pump(app, 1)
    code1 = app.pairing["user_code"]
    server.world.expire_all()
    clock.advance(2)
    pump(app, 2)
    assert app.state == S.EXPIRED and app.pairing is None
    app.handle_key("enter")
    pump(app, 1)
    assert app.state == S.SHOW_CODE and app.pairing["user_code"] != code1


def test_local_expiry_uses_monotonic_clock_not_wall_clock(make_app, server, clock, monkeypatch):
    app = make_app()
    monkeypatch.setattr(time, "time", lambda: 4_000_000_000.0)  # wall clock wildly skewed
    app.start()
    pump(app, 1)
    assert app.state == S.SHOW_CODE
    clock.advance(601)
    pump(app, 1)
    assert app.state == S.EXPIRED


def test_poll_429_backs_off(make_app, server, clock):
    app = make_app()
    app.start()
    pump(app, 1)
    server.world.faults["token"].append(429)
    clock.advance(1.5)
    pump(app, 2)
    assert app.state == S.SHOW_CODE
    assert app.next_poll_at - clock() >= 2  # honours Retry-After: 2


def test_code_request_503_retries(make_app, server, clock):
    server.world.faults["code"].append(503)
    app = make_app()
    app.start()
    pump(app, 1)
    assert app.state == S.UNPAIRED and app.message
    clock.advance(5)
    pump(app, 2)
    assert app.state == S.SHOW_CODE


def test_full_pair_helper(make_app, server, clock):
    app = pair(make_app(), server, clock)
    assert app.state == S.PLAY
    assert app.snapshot["state"]["location"]["name"] == "The Lantern Tavern"
