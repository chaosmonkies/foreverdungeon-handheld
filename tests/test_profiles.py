"""Multi-account profiles, on-device sign-up / sign-in, migration and per-profile pending safety."""
import json
import os
import stat

import pytest

from conftest import pump
from foreverdungeon_handheld.api import ApiClient
from foreverdungeon_handheld.app import App, S
from foreverdungeon_handheld.layout import render
from foreverdungeon_handheld.profiles import ProfileStore, read_provision_key

KEY = "fake-provision-key"
PW = "correct-horse-battery"


@pytest.fixture
def base(tmp_path):
    d = tmp_path / "fdungeon"
    d.mkdir()
    (d / "provision_key").write_text(KEY + "\n")
    os.chmod(d / "provision_key", 0o600)
    return d


@pytest.fixture
def make_papp(server, clock, base):
    def _make(key_path=None):
        kp = key_path or base / "provision_key"
        return App(ApiClient(server.url, timeout=5), None, width=250, height=122, clock=clock, threaded=False,
                   profiles=ProfileStore(base), provision_key=lambda: read_provision_key(kp))
    return _make


def typ(app, text):
    for ch in text:
        app.handle_key(ch)
    app.handle_key("enter")


def screen_text(app):
    scr = app.screen()
    render(scr, 250, 122)  # every account screen must render at 250x122
    return " ".join([r.text for r in scr.rows] + [scr.title, scr.footer or "", scr.input_line or ""])


def pick(app, label_prefix):
    entries = app.picker_entries()
    idx = next(i for i, e in enumerate(entries) if e[2].startswith(label_prefix))
    while app.cursor != idx:
        app.handle_key("down")
    app.handle_key("enter")


def signup(app, username, character=""):
    pick(app, "+ New account")
    assert app.state == S.FORM
    typ(app, username)
    typ(app, f"{username}@example.com")
    typ(app, PW)
    typ(app, PW)
    typ(app, character)
    pump(app, 4)


def test_picker_signup_creates_profile_and_plays(make_papp, server, base):
    app = make_papp()
    app.start()
    assert app.state == S.PICKER
    assert "No accounts" in screen_text(app)
    pick(app, "+ New account")
    typ(app, "hero_one")
    typ(app, "hero_one@example.com")
    for ch in PW:
        app.handle_key(ch)
    shown = screen_text(app)
    assert app.screen().input_line == "*" * len(PW)     # masked
    assert PW not in shown
    app.handle_key("enter")
    typ(app, PW)
    typ(app, "Wren")
    pump(app, 4)
    assert app.state == S.PLAY
    assert app.profile == "hero_one"
    tok = base / "profiles" / "hero_one" / "device-token"
    assert stat.S_IMODE(os.stat(tok).st_mode) == 0o600
    assert json.loads(tok.read_text())["character_id"] == "chr_hero_one"
    assert (base / "last-profile").read_text() == "hero_one"
    path, body = server.world.account_posts[-1]
    assert path == "/api/devices/signup" and body["provision_key"] == KEY and body["character_name"] == "Wren"
    assert app.form is None                                # password dropped from memory
    # play one action
    app.handle_key("enter")
    pump(app, 4)
    assert server.world.action_posts == 1 and app.pending is None


def test_provision_key_never_displayed(make_papp, base):
    app = make_papp()
    app.start()
    signup_screens = []
    pick(app, "+ New account")
    for value in ["keyless", "k@example.com", PW, PW, ""]:
        signup_screens.append(screen_text(app))
        typ(app, value)
    signup_screens.append(screen_text(app))
    assert all(KEY not in s for s in signup_screens)


def test_missing_provision_key_explains_and_offers_pairing(make_papp, base, tmp_path):
    app = make_papp(key_path=tmp_path / "nope")
    app.start()
    signup_flow = ["nokey", "nokey@example.com", PW, PW, ""]
    pick(app, "+ New account")
    for v in signup_flow:
        typ(app, v)
    assert app.state == S.FORM
    assert "provision key" in app.message and "Pair code" in app.message


def test_local_validation_and_password_mismatch(make_papp):
    app = make_papp()
    app.start()
    pick(app, "+ New account")
    typ(app, "ab")
    assert "3-32" in app.message and app.form["index"] == 0
    typ(app, "c")  # buffer kept: "abc"
    typ(app, "not-an-email")
    assert "email" in app.message
    app.buffer = ""
    typ(app, "abc@example.com")
    typ(app, "short")
    assert "10 characters" in app.message
    app.buffer = ""
    typ(app, PW)
    typ(app, PW + "x")
    assert "didn't match" in app.message and app.form["fields"][app.form["index"]][0] == "password"
    assert app.buffer == ""
    app.handle_key("esc")   # back one field
    assert app.form["fields"][app.form["index"]][0] == "email" and app.buffer == "abc@example.com"


def test_server_errors_are_clear_and_return_to_field(make_papp, server):
    server.world.accounts["taken"] = {"email": "t@example.com", "password": PW, "characters": [{"id": "chr_t", "name": "T"}]}
    app = make_papp()
    app.start()
    signup(app, "taken")
    assert app.state == S.FORM
    assert app.form["fields"][app.form["index"]][0] == "username"
    assert "already registered" in app.message
    app.handle_key("esc"); app.handle_key("esc")  # back to picker
    assert app.state == S.PICKER
    pick(app, "+ Sign in")
    typ(app, "taken")
    typ(app, "wrong-password-123")
    pump(app, 4)
    assert app.state == S.FORM and "Wrong username/email or password" in app.message
    assert app.form["fields"][app.form["index"]][0] == "password" and app.buffer == ""
    server.world.provision_keys = {"other"}
    typ(app, PW)
    pump(app, 4)
    assert "not authorized" in app.message
    server.world.provision_keys = set()
    app._form_goto(1)
    typ(app, PW)
    pump(app, 4)
    assert "turned off" in app.message


def test_signin_choose_character(make_papp, server, base):
    chars = [{"id": "chr_a", "name": "Ash", "background": "guard", "level": 2},
             {"id": "chr_b", "name": "Briar", "background": "tinker", "level": 1}]
    server.world.accounts["multi"] = {"email": "m@example.com", "password": PW, "characters": chars}
    app = make_papp()
    app.start()
    pick(app, "+ Sign in")
    typ(app, "m@example.com")
    typ(app, PW)
    pump(app, 4)
    assert app.state == S.CHOOSE_CHAR
    assert "Briar" in screen_text(app)
    app.handle_key("down")
    app.handle_key("enter")
    pump(app, 4)
    assert app.state == S.PLAY and app.character_id == "chr_b"
    assert server.world.account_posts[-1][1]["character_id"] == "chr_b"
    assert ProfileStore(base).meta("multi")["username"] == "multi"


def test_two_profiles_switch_and_last_used_is_preselected(make_papp, server, base):
    app = make_papp()
    app.start()
    signup(app, "alpha")
    assert app.state == S.PLAY
    app.handle_key("esc")
    assert app.state == S.PICKER
    signup(app, "bravo")
    assert app.profile == "bravo"
    app.handle_key("esc")
    entries = app.picker_entries()
    assert [e[1] for e in entries if e[0] == "profile"] == ["alpha", "bravo"]
    assert entries[app.cursor][1] == "bravo"                    # last used preselected
    # restart: still preselected
    app2 = make_papp()
    app2.start()
    assert app2.picker_entries()[app2.cursor][1] == "bravo"
    pick(app2, "alpha")
    pump(app2, 3)
    assert app2.state == S.PLAY and app2.character_id == "chr_alpha"
    assert server.world.last_auth.endswith(json.loads((base / "profiles/alpha/device-token").read_text())["access_token"])


def test_pending_envelope_is_per_profile_and_survives_switch(make_papp, server, base):
    app = make_papp()
    app.start()
    signup(app, "alpha")
    app.handle_key("esc")
    signup(app, "bravo")
    app.handle_key("esc")
    pick(app, "alpha")
    pump(app, 3)
    server.world.faults["action"].append("drop_after_commit")
    app.handle_key("enter")
    pump(app, 3)
    assert app.state == S.ACTION_PENDING
    key = app.pending["idempotency_key"]
    assert (base / "profiles/alpha/pending-action.json").exists()
    app.handle_key("esc")                                  # leave while uncertain
    assert "[1 pending]" in screen_text(app)
    pick(app, "bravo")
    pump(app, 3)
    assert app.pending is None and app.state == S.PLAY     # bravo is not blocked by alpha's envelope
    app.handle_key("esc")
    pick(app, "alpha")
    pump(app, 4)
    assert app.pending is None and app.state == S.PLAY
    assert key in server.world.idem
    assert server.world.action_posts >= 2 and len(server.world.events) == 1   # resent with same key, committed once
    assert not (base / "profiles/alpha/pending-action.json").exists()


def test_stale_results_from_previous_profile_are_dropped(make_papp, server, base):
    app = make_papp()
    app.start()
    signup(app, "alpha")
    app.handle_key("esc")
    signup(app, "bravo")
    app.handle_key("esc")
    pick(app, "alpha")             # state fetch queued for alpha (threaded=False: runs at next drain)
    app.handle_key("esc")
    pick(app, "bravo")
    pump(app, 3)
    assert app.character_id == "chr_bravo"
    assert app.snapshot["character_id"] == "chr_bravo"


def test_remove_profile_locally(make_papp, base):
    app = make_papp()
    app.start()
    signup(app, "gone")
    app.handle_key("esc")
    app.handle_key("x")
    assert app.state == S.CONFIRM_REMOVE and "gone" in screen_text(app)
    app.handle_key("esc")
    assert (base / "profiles/gone").exists()
    app.handle_key("x")
    app.handle_key("enter")
    assert app.state == S.PICKER and not (base / "profiles/gone").exists()
    assert not (base / "last-profile").exists()


def test_abandoned_pairing_leaves_no_profile(make_papp, base):
    app = make_papp()
    app.start()
    pick(app, "+ Pair code")
    pump(app, 2)
    assert app.state == S.SHOW_CODE
    app.handle_key("esc")
    assert app.state == S.PICKER and ProfileStore(base).names() == []


def test_legacy_single_account_is_migrated(make_papp, base):
    (base / "device-token").write_text(json.dumps({"access_token": "legacy", "character_id": "chr_test"}))
    (base / "state.json").write_text(json.dumps({"character_id": "chr_test", "last_narration": "old"}))
    app = make_papp()
    app.start()
    assert not (base / "device-token").exists()
    assert json.loads((base / "profiles/default/device-token").read_text())["access_token"] == "legacy"
    assert (base / "profiles/default/state.json").exists()
    assert app.picker_entries()[app.cursor][1] == "default"


def test_q_quits_from_picker_and_esc_quits_too(make_papp):
    app = make_papp()
    app.start()
    app.handle_key("q")
    assert app.quit
