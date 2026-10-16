"""TOML configuration with environment overrides for non-secret settings."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_BASE_URL = "https://foreverdungeon.moonshinelabs.io"


@dataclass
class ServerConfig:
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 25.0
    state_poll_seconds: float = 30.0
    device_name: str = "E-paper handheld"


@dataclass
class DisplayConfig:
    driver: str = "simulator"
    width: int = 400
    height: int = 300
    rotation: int = 0
    partial_refresh: bool = True
    full_refresh_every: int = 8
    min_partial_seconds: float = 1.5
    min_full_seconds: float = 8.0
    scale: int = 2


@dataclass
class InputConfig:
    driver: str = "keyboard"
    text_entry: bool = True
    debounce_ms: int = 40
    long_press_ms: int = 700
    active_low: bool = True
    pull_up: bool = True
    pins: dict = field(default_factory=dict)  # action name -> GPIO line, e.g. {"UP": 5}


@dataclass
class CacheConfig:
    path: str = "~/.local/state/foreverdungeon-handheld/state.json"
    token_path: str = "~/.local/state/foreverdungeon-handheld/device-token"
    pending_path: str = ""  # default: <state dir>/pending-action.json
    max_recent_events: int = 30


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    input: InputConfig = field(default_factory=InputConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)


def _coerce(value: str, current):
    if isinstance(current, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    return value


def _apply(section, data: dict) -> None:
    names = {f.name for f in fields(section)}
    for key, value in data.items():
        if key in names:
            setattr(section, key, value)


def load(path: str | os.PathLike | None = None, env: dict | None = None) -> Config:
    cfg = Config()
    if path:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        for name in ("server", "display", "input", "cache"):
            _apply(getattr(cfg, name), data.get(name, {}))
    env = os.environ if env is None else env
    # FDH_<SECTION>_<KEY>, e.g. FDH_SERVER_BASE_URL, FDH_DISPLAY_WIDTH. Tokens are never read from env.
    for name in ("server", "display", "input", "cache"):
        section = getattr(cfg, name)
        for f in fields(section):
            key = f"FDH_{name.upper()}_{f.name.upper()}"
            if key in env and not isinstance(getattr(section, f.name), dict):
                setattr(section, f.name, _coerce(env[key], getattr(section, f.name)))
    cfg.server.base_url = cfg.server.base_url.rstrip("/")
    for attr in ("path", "token_path", "pending_path"):
        val = getattr(cfg.cache, attr)
        if val:
            setattr(cfg.cache, attr, str(Path(val).expanduser()))
    if not cfg.cache.pending_path:
        cfg.cache.pending_path = str(Path(cfg.cache.path).with_name("pending-action.json"))
    return cfg
