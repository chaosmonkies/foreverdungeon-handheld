"""Standalone entry point: `foreverdungeon-handheld --config file.toml`.

Drivers: `simulator` (Tkinter window) or `headless` (PNG frames to --frames-dir).
The physical LAFVIN 2.13" panel is driven only through PiPanel (displays/pipanel.py).
"""
from __future__ import annotations

import argparse
import signal
import sys
import time

from . import config as cfgmod
from .api import ApiClient
from .app import App
from .cache import Cache
from .displays.base import RefreshPolicy
from .layout import render


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="foreverdungeon-handheld")
    ap.add_argument("--config")
    ap.add_argument("--driver", choices=["simulator", "headless"])
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--frames-dir", default="frames")
    args = ap.parse_args(argv)
    cfg = cfgmod.load(args.config)
    d = cfg.display
    driver = args.driver or d.driver
    w, h = args.width or d.width, args.height or d.height
    if d.rotation in (90, 270):
        w, h = h, w
    if driver == "simulator":
        from .displays.simulator import TkSimulator
        disp = TkSimulator(w, h, d.scale)
    elif driver == "headless":
        from .displays.simulator import HeadlessDisplay
        disp = HeadlessDisplay(w, h, args.frames_dir)
    else:
        print(f"Unknown display driver {driver!r}. The e-paper panel runs via PiPanel.", file=sys.stderr)
        return 2
    disp.initialize()
    api = ApiClient(cfg.server.base_url, cfg.server.timeout_seconds)
    cache = Cache(cfg.cache.path, cfg.cache.token_path, cfg.cache.pending_path, cfg.cache.max_recent_events)
    app = App(api, cache, width=w, height=h, device_name=cfg.server.device_name,
              text_entry=cfg.input.text_entry, state_poll_seconds=cfg.server.state_poll_seconds)
    policy = RefreshPolicy(supports_partial=d.partial_refresh and disp.supports_partial,
                           full_every=d.full_refresh_every, min_partial=d.min_partial_seconds,
                           min_full=d.min_full_seconds)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    app.start()
    last_refresh = 0
    try:
        while not app.quit and not stop:
            keys = disp.poll_keys() if hasattr(disp, "poll_keys") else []
            if getattr(disp, "closed", False):
                break
            for k in keys:
                app.handle_key(k)
            app.tick()
            scr = app.screen()
            img, _, _ = render(scr, w, h)
            force = app.manual_refresh != last_refresh
            last_refresh = app.manual_refresh
            mode = policy.decide(img, scr.key, force_full=force)
            if mode:
                disp.render(img, full=mode == "full")
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        disp.sleep()  # leave the last useful page on the panel
        disp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
