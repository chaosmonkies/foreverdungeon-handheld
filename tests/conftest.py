import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from fakeserver import FakeServer  # noqa: E402
from foreverdungeon_handheld.api import ApiClient  # noqa: E402
from foreverdungeon_handheld.app import App  # noqa: E402
from foreverdungeon_handheld.cache import Cache  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def server():
    with FakeServer() as s:
        yield s


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_cache(tmp_path):
    def _make(sub="state"):
        d = tmp_path / sub
        return Cache(d / "state.json", d / "device-token", d / "pending-action.json")
    return _make


@pytest.fixture
def make_app(server, clock, make_cache):
    def _make(cache=None, w=250, h=122):
        cache = cache or make_cache()
        api = ApiClient(server.url, timeout=5)
        return App(api, cache, width=w, height=h, clock=clock, threaded=False)
    return _make


def pump(app, n=6):
    for _ in range(n):
        app.tick()


def pair(app, server, clock):
    app.start()
    pump(app, 2)
    server.world.approve()
    clock.advance(2)
    pump(app, 4)
    return app
