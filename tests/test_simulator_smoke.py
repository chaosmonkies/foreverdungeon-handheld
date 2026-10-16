import os

from PIL import Image

from conftest import pair, pump
from foreverdungeon_handheld.displays.base import Display, RefreshPolicy
from foreverdungeon_handheld.displays.simulator import HeadlessDisplay
from foreverdungeon_handheld.input import Action, Debouncer, translate
from foreverdungeon_handheld.layout import render
from pipanel_harness import Btn, Disp


def test_headless_display_protocol(tmp_path):
    d = HeadlessDisplay(250, 122, str(tmp_path))
    assert isinstance(d, Display)
    d.initialize()
    d.render(Image.new("1", (250, 122), 255), full=True)
    assert len(list(tmp_path.iterdir())) == 1


def test_refresh_policy(clock):
    p = RefreshPolicy(supports_partial=True, full_every=3, min_partial=1.5, min_full=8, clock=clock)
    a = Image.new("1", (10, 10), 255)
    b = Image.new("1", (10, 10), 0)
    assert p.decide(a, "k") == "full"
    assert p.decide(a, "k") is None                    # identical pixels: no refresh
    assert p.decide(b, "k") is None                    # too soon: deferred/coalesced
    clock.advance(2)
    assert p.decide(b, "k") == "partial"
    for i in range(2):
        clock.advance(2)
        assert p.decide(a if i % 2 == 0 else b, "k") == "partial"
    clock.advance(9)
    assert p.decide(a, "k") == "full"                  # every N partials
    clock.advance(9)
    assert p.decide(b, "new-location") == "full"       # layout/location change
    clock.advance(2)
    assert p.decide(a, "other") == "partial"           # full wanted but too soon: partial now ...
    assert p.decide(a, "other") is None
    clock.advance(8)
    assert p.decide(a, "other") == "full"              # ... and the owed full refresh follows
    assert p.decide(a, "other") is None
    nop = RefreshPolicy(supports_partial=False, clock=clock)
    assert nop.decide(a) == "full"
    clock.advance(1)
    assert nop.decide(b) is None                       # full-only panel waits for min_full
    clock.advance(9)
    assert nop.decide(b) == "full"


def test_debounce_and_hold(clock):
    d = Debouncer(40, 700, clock=clock)
    assert d.press(Action.DOWN)
    clock.advance(0.01)
    assert not d.press(Action.DOWN)     # bounce dropped
    clock.advance(0.1)
    assert d.press(Action.DOWN)
    assert not d.hold(Action.DOWN)
    clock.advance(0.8)
    assert d.hold(Action.DOWN)          # long press auto-repeat
    assert not d.hold(Action.DOWN)      # rate limited
    d.release(Action.DOWN)
    assert not d.hold(Action.DOWN)
    assert translate("enter") == (Action.SELECT, None)
    assert translate("x") == (Action.CHAR, "x")


def test_full_session_renders_every_size(make_app, server, clock, tmp_path):
    for w, h in [(200, 200), (250, 122), (296, 128), (400, 300), (800, 480)]:
        app = pair(make_app(w=w, h=h), server, clock)
        for key in ["down", "down", "right", "i", "enter", "m", "m", "esc", "t", "x"]:
            app.handle_key(key)
            img, pi, n = render(app.screen(), w, h)
            assert img.size == (w, h)
        img.save(tmp_path / f"{w}x{h}.png")


def test_pipanel_harness_against_fake_server(server, tmp_path):
    """Drives pipanel.run() exactly as PiPanel would, with a worker thread and real time."""
    from foreverdungeon_handheld.displays import pipanel

    out = tmp_path / "shots"
    d = Disp(str(out))
    approved = {}

    def hook(i):
        if i == 12 and not approved:
            server.world.approve()
            approved["x"] = True

    # idle while pairing (poll_interval=1), then pick choices, open menus, type a command
    # account picker first: "+ New account", "+ Sign in", "+ Pair code"
    seq = ["down", "down", "enter"] + ["_"] * 30 + ["down", "down", "enter"] + ["_"] * 15 + ["i", "_", "_", "enter", "_", "esc", "j", "_", "esc",
           "w", "_", "esc", "t", "h", "u", "m", "enter"] + ["_"] * 15
    app = pipanel.run(d, Btn(seq, 0.1, hook), base_dir=str(tmp_path / "fdungeon"), base_url=server.url,
                      min_partial=0.0, min_full=0.0)
    shots = sorted(os.listdir(out))
    assert d.fulls[0] is True
    assert any("full" in s for s in shots)
    assert os.stat(tmp_path / "fdungeon" / "profiles" / "paired" / "device-token").st_mode & 0o777 == 0o600
    assert (tmp_path / "fdungeon" / "last-profile").read_text() == "paired"
    assert os.stat(tmp_path / "fdungeon").st_mode & 0o777 == 0o700
    assert server.world.gold == 11                         # "take copper coin" applied once
    assert server.world.events[-1]["summary"].startswith("You hum")
    assert app.pending is None
    # the first frame is the account picker
    first = Image.open(out / shots[0])
    assert first.size == (250, 122) and first.mode == "1"
