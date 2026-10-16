"""Input abstraction: logical actions, keyboard mapping, debounce, optional GPIO buttons.

Pins, polarity, pulls, debounce and long-press thresholds are configuration;
application logic only ever sees `Action` values (plus text characters when a
keyboard is present).
"""
from __future__ import annotations

import enum
import time


class Action(enum.Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"         # previous page
    RIGHT = "right"       # next page
    SELECT = "select"
    BACK = "back"
    MENU = "menu"         # cycle menus (inventory / journal / world)
    REFRESH = "refresh"
    TEXT = "text"         # open command entry (keyboard only)
    QUIT = "quit"
    INVENTORY = "inventory"
    JOURNAL = "journal"
    WORLD = "world"
    CHAR = "char"         # a printable character (command entry / shortcuts)
    BACKSPACE = "backspace"


# Names used by PiPanel's egkit.keys() and the simulator.
KEY_NAMES = {
    "up": Action.UP, "down": Action.DOWN, "left": Action.LEFT, "right": Action.RIGHT,
    "enter": Action.SELECT, "return": Action.SELECT, "esc": Action.BACK, "escape": Action.BACK,
    "back": Action.BACKSPACE, "backspace": Action.BACKSPACE, "tab": Action.MENU,
    "pageup": Action.LEFT, "pagedown": Action.RIGHT, "f5": Action.REFRESH,
}

# Single-character shortcuts outside command entry.
CHAR_SHORTCUTS = {
    "t": Action.TEXT, "/": Action.TEXT, "i": Action.INVENTORY, "j": Action.JOURNAL,
    "w": Action.WORLD, "m": Action.MENU, "r": Action.REFRESH, "q": Action.QUIT,
}


def translate(key: str) -> tuple[Action, str | None]:
    """Map a raw key name or single character to (Action, char)."""
    if key in KEY_NAMES:
        return KEY_NAMES[key], None
    if isinstance(key, str) and len(key) == 1:
        return Action.CHAR, key
    return Action.CHAR, None


class Debouncer:
    """Drop repeats of the same logical action within `debounce_ms` (contact bounce).

    A held button repeats only after `long_press_ms`, then at most every
    `long_press_ms / 3`, so a stuck/held key cannot flood the state machine.
    """

    def __init__(self, debounce_ms: int = 40, long_press_ms: int = 700, clock=time.monotonic):
        self.debounce = debounce_ms / 1000.0
        self.long = long_press_ms / 1000.0
        self.clock = clock
        self._last: dict = {}
        self._held_since: dict = {}

    def press(self, action) -> bool:
        now = self.clock()
        last = self._last.get(action)
        if last is not None and now - last < self.debounce:
            return False
        self._last[action] = now
        self._held_since[action] = now
        return True

    def hold(self, action) -> bool:
        """Called while a button stays down; returns True when an auto-repeat should fire."""
        now = self.clock()
        start = self._held_since.get(action)
        if start is None:
            return False
        if now - start >= self.long and now - self._last.get(action, 0) >= self.long / 3:
            self._last[action] = now
            return True
        return False

    def release(self, action) -> None:
        self._held_since.pop(action, None)


class GpioButtons:
    """Optional gpiozero buttons. Pins come from config: {"UP": 5, "DOWN": 6, ...}.

    Not used by the PiPanel embedding (PiPanel owns its own input). Untested on hardware.
    """

    def __init__(self, pins: dict, active_low: bool = True, pull_up: bool = True, debounce_ms: int = 40):
        from gpiozero import Button  # lazy: not needed for simulator/tests

        self.queue: list[Action] = []
        self.buttons = []
        for name, pin in pins.items():
            action = Action[name.upper()]
            if pull_up:  # internal pull: up for active-low, down for active-high
                b = Button(pin, pull_up=active_low, bounce_time=debounce_ms / 1000.0)
            else:        # external resistor: tell gpiozero which level means pressed
                b = Button(pin, pull_up=None, active_state=not active_low, bounce_time=debounce_ms / 1000.0)
            b.when_pressed = lambda a=action: self.queue.append(a)
            self.buttons.append(b)

    def poll(self) -> list[Action]:
        out, self.queue = self.queue, []
        return out
