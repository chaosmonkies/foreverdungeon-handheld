"""Simulator drivers.

`HeadlessDisplay` keeps (and optionally saves) every frame: used by tests.
`TkSimulator` shows the exact 1-bit bitmap, scaled, with a border that is
black after a FULL refresh and dashed/grey after a PARTIAL one plus a counter.
"""
from __future__ import annotations

import os

from PIL import Image


class HeadlessDisplay:
    supports_partial = True

    def __init__(self, width: int, height: int, out_dir: str | None = None, prefix: str = "frame"):
        self.width, self.height = width, height
        self.out_dir = out_dir
        self.prefix = prefix
        self.frames: list[tuple[Image.Image, bool]] = []

    def initialize(self) -> None:
        if self.out_dir:
            os.makedirs(self.out_dir, exist_ok=True)

    def render(self, image: Image.Image, *, full: bool) -> None:
        assert image.mode == "1" and image.size == (self.width, self.height)
        self.frames.append((image.copy(), full))
        if self.out_dir:
            image.save(os.path.join(self.out_dir, f"{self.prefix}-{len(self.frames):03d}{'-full' if full else ''}.png"))

    def sleep(self) -> None:
        pass

    def close(self) -> None:
        pass


TK_KEYS = {"Up": "up", "Down": "down", "Left": "left", "Right": "right", "Return": "enter",
           "Escape": "esc", "BackSpace": "back", "Tab": "tab", "Prior": "pageup", "Next": "pagedown", "F5": "f5"}


class TkSimulator:
    supports_partial = True

    def __init__(self, width: int, height: int, scale: int = 2):
        self.width, self.height, self.scale = width, height, scale
        self.keys: list[str] = []
        self.full_count = self.partial_count = 0
        self.closed = False

    def initialize(self) -> None:
        import tkinter as tk  # lazy: tests never import tkinter

        self.tk = tk
        self.root = tk.Tk()
        self.root.title(f"Forever Dungeon handheld {self.width}x{self.height}")
        self.canvas = tk.Canvas(self.root, width=self.width * self.scale + 8, height=self.height * self.scale + 8,
                                highlightthickness=0, bg="#999")
        self.canvas.pack()
        self.label = tk.Label(self.root, text="", font=("Courier", 11))
        self.label.pack(fill="x")
        self.root.bind("<Key>", self._key)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._photo = None

    def _key(self, ev):
        self.keys.append(TK_KEYS.get(ev.keysym, ev.char if ev.char and ev.char.isprintable() else ""))

    def _close(self):
        self.closed = True

    def poll_keys(self) -> list[str]:
        self.root.update()
        out = [k for k in self.keys if k]
        self.keys = []
        return out

    def render(self, image: Image.Image, *, full: bool) -> None:
        if full:
            self.full_count += 1
        else:
            self.partial_count += 1
        big = image.convert("L").resize((self.width * self.scale, self.height * self.scale), Image.NEAREST)
        data = big.tobytes()
        w, h = big.size
        ppm = f"P5 {w} {h} 255 ".encode() + data
        self._photo = self.tk.PhotoImage(data=ppm, format="PPM")
        self.canvas.delete("all")
        color = "#000" if full else "#d33"
        self.canvas.create_rectangle(0, 0, w + 7, h + 7, outline=color, width=4, dash=() if full else (4, 2))
        self.canvas.create_image(4, 4, image=self._photo, anchor="nw")
        self.label.config(text=f"{'FULL' if full else 'partial'} refresh | full={self.full_count} partial={self.partial_count}")
        self.root.update()

    def sleep(self) -> None:
        pass

    def close(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass
