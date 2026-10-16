"""PiPanel embedding: `run(disp, btn)` for the 250x122 LAFVIN 2.13" e-paper on the Orange Pi Zero 2W.

PiPanel (/root/panel) owns the SPI panel and the input devices; this module
never touches GPIO/SPI. Contract (see PiPanel egames/egkit.py):
  disp.w, disp.h          -> 250, 122
  disp.new()              -> PIL "1" image, white=255
  disp.show(img, full=b)  -> queue a partial (~0.6 s) or full (~2.3 s) refresh
  egkit.keys(btn, t)      -> ["up","down","left","right","enter","esc","back", single chars]

Network runs on a worker thread; this loop only composes frames and calls
disp.show() from one thread, so a refresh is never interrupted by I/O.
State lives in /root/panel/fdungeon/: one directory per account under
profiles/<name>/ (token 0600, cache, pending envelope), last-profile, and the
0600 provision_key used for on-device sign-up/sign-in (never displayed).
Override with FDH_PIPANEL_DIR (used for the /tmp smoke test) and FDH_SERVER_BASE_URL.
"""
from __future__ import annotations

import os
import time

from ..api import ApiClient
from ..app import App
from ..config import DEFAULT_BASE_URL
from ..profiles import ProfileStore, read_provision_key
from ..layout import render
from .base import RefreshPolicy

DEFAULT_DIR = "/root/panel/fdungeon"
_PAD = {"UP": "up", "DOWN": "down", "LEFT": "left", "RIGHT": "right", "A": "enter",
        "START": "enter", "SELECT": "esc", "B": "esc"}


def _keys(btn, timeout=0.1):
    try:
        import egkit  # PiPanel's helper when it is importable
        return egkit.keys(btn, timeout)
    except ImportError:
        out = []
        for nm, val in btn.poll(timeout=0.0):
            if val == 1:
                out.append(_PAD.get(nm, nm))
        for kind, ch in btn.text(timeout=timeout):
            out.append(ch if kind == "char" else kind)
        return out


def run(disp, btn, *, base_dir: str | None = None, base_url: str | None = None, api: ApiClient | None = None,
        min_partial: float = 0.7, min_full: float = 4.0):
    base = base_dir or os.environ.get("FDH_PIPANEL_DIR", DEFAULT_DIR)
    os.makedirs(base, mode=0o700, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    url = base_url or os.environ.get("FDH_SERVER_BASE_URL", DEFAULT_BASE_URL)
    api = api or ApiClient(url, timeout=25.0)
    profiles = ProfileStore(base)
    key_path = os.path.join(base, "provision_key")
    w, h = int(getattr(disp, "w", 250)), int(getattr(disp, "h", 122))
    app = App(api, None, width=w, height=h, device_name="PiPanel e-paper handheld", text_entry=True,
              state_poll_seconds=30.0, profiles=profiles, provision_key=lambda: read_provision_key(key_path))
    policy = RefreshPolicy(supports_partial=True, full_every=8, min_partial=min_partial, min_full=min_full)
    if hasattr(btn, "flush"):
        btn.flush()
    app.start()
    try:
        while not app.quit:
            for k in _keys(btn, 0.1):
                app.handle_key(k)
                if app.quit:
                    break
            if app.quit:
                break
            app.tick()
            scr = app.screen()
            img, _, _ = render(scr, w, h)
            mode = policy.decide(img, scr.key)
            if mode:
                canvas = disp.new()
                canvas.paste(img)
                disp.show(canvas, full=(mode == "full"))
    finally:
        app.stop()  # worker is a daemon thread; a pending envelope stays on disk for next launch
    return app
