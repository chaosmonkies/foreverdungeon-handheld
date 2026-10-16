"""Display protocol and the e-paper refresh policy shared by every driver."""
from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from PIL import Image


@runtime_checkable
class Display(Protocol):
    width: int
    height: int
    supports_partial: bool

    def initialize(self) -> None: ...
    def render(self, image: Image.Image, *, full: bool) -> None: ...
    def sleep(self) -> None: ...
    def close(self) -> None: ...


class RefreshPolicy:
    """Decides whether a composed frame is drawn, and how.

    - identical pixels are never redrawn
    - full refresh on first frame, when the screen key changes (boot, pairing
      code, location, layout), and every `full_every` partial refreshes
    - partial refresh only if the driver declares it safe
    - minimum intervals between refreshes; a too-early frame is deferred
      (coalesced) rather than dropped: call `decide` again on the next tick
    """

    def __init__(self, *, supports_partial: bool, full_every: int = 8, min_partial: float = 1.5,
                 min_full: float = 8.0, clock=time.monotonic):
        self.supports_partial = supports_partial
        self.full_every = max(1, full_every)
        self.min_partial = min_partial
        self.min_full = min_full
        self.clock = clock
        self.last_bytes: bytes | None = None
        self.last_key = None
        self.last_at = -1e9
        self.last_full_at = -1e9
        self.partials = 0
        self.full_count = 0
        self.partial_count = 0
        self.owed_full = False  # a full refresh was downgraded/deferred; do it when allowed

    def decide(self, image: Image.Image, key=None, force_full: bool = False) -> str | None:
        """Returns "full", "partial", or None (nothing to draw now)."""
        data = image.tobytes()
        now = self.clock()
        if data == self.last_bytes and not force_full:
            if self.owed_full and now - self.last_full_at >= self.min_full and now - self.last_at >= self.min_partial:
                self.owed_full = False
                self.last_at = self.last_full_at = now
                self.partials = 0
                self.full_count += 1
                return "full"
            return None
        want_full = (force_full or self.last_bytes is None or key != self.last_key
                     or not self.supports_partial or self.partials >= self.full_every)
        if want_full:
            # a full refresh too soon after another one: fall back to partial if allowed, else wait
            if now - self.last_full_at < self.min_full and self.last_bytes is not None and not force_full:
                if self.supports_partial and now - self.last_at >= self.min_partial:
                    want_full = False
                    self.owed_full = True
                else:
                    return None
        elif now - self.last_at < self.min_partial:
            return None
        self.last_bytes = data
        self.last_key = key
        self.last_at = now
        if want_full:
            self.owed_full = False
            self.last_full_at = now
            self.partials = 0
            self.full_count += 1
            return "full"
        self.partials += 1
        self.partial_count += 1
        return "partial"
