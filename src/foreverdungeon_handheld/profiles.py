"""Local account profiles: one directory per account, each with its own token, cache and pending envelope.

    <base>/profiles/<name>/device-token        0600, {"access_token", "character_id"}
    <base>/profiles/<name>/state.json          last server snapshot (offline view)
    <base>/profiles/<name>/pending-action.json at most one unsent/uncertain envelope
    <base>/profiles/<name>/profile.json        {"username", "character_name"} labels only
    <base>/last-profile                        name of the profile to preselect
    <base>/provision_key                       0600, installed by scripts; never shown or logged

A legacy single-account layout (<base>/device-token etc.) is moved into a profile on first use.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .cache import Cache, atomic_write

LEGACY_FILES = ("device-token", "state.json", "pending-action.json")
_NAME_RE = re.compile(r"[^a-z0-9_-]+")


def sanitize(name: str) -> str:
    clean = _NAME_RE.sub("-", (name or "").strip().lower()).strip("-")[:32]
    return clean or "account"


def read_provision_key(path) -> str | None:
    try:
        key = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return key or None


class ProfileStore:
    def __init__(self, base_dir, max_recent_events: int = 30):
        self.base = Path(base_dir)
        self.root = self.base / "profiles"
        self.max_recent_events = max_recent_events

    # ---- layout ---------------------------------------------------------
    def _dir(self, name: str) -> Path:
        if name != sanitize(name):
            raise ValueError("bad profile name")
        return self.root / name

    def _mkdir(self, path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    def cache_for(self, name: str) -> Cache:
        d = self._dir(name)
        self._mkdir(self.root)
        self._mkdir(d)
        return Cache(d / "state.json", d / "device-token", d / "pending-action.json", self.max_recent_events)

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_dir() and not p.name.startswith(".") and p.name == sanitize(p.name))

    def meta(self, name: str) -> dict:
        try:
            data = json.loads((self._dir(name) / "profile.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def update_meta(self, name: str, **values) -> None:
        current = self.meta(name)
        merged = dict(current, **{k: v for k, v in values.items() if v})
        if merged != current:
            self._mkdir(self._dir(name))
            atomic_write(self._dir(name) / "profile.json", json.dumps(merged).encode("utf-8"))

    def has_token(self, name: str) -> bool:
        return (self._dir(name) / "device-token").exists()

    def has_pending(self, name: str) -> bool:
        return (self._dir(name) / "pending-action.json").exists()

    # ---- naming -----------------------------------------------------------
    def unique_name(self, wanted: str) -> str:
        base = sanitize(wanted)
        existing = set(self.names())
        if base not in existing:
            return base
        n = 2
        while f"{base[:28]}-{n}" in existing:
            n += 1
        return f"{base[:28]}-{n}"

    def name_for_username(self, username: str) -> str:
        """Reuse the profile already holding this account (re-sign-in after revoke), else a new name."""
        for name in self.names():
            if str(self.meta(name).get("username", "")).casefold() == username.casefold():
                return name
        return self.unique_name(username)

    # ---- last used ----------------------------------------------------------
    def last(self) -> str | None:
        try:
            name = (self.base / "last-profile").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return name if name in self.names() else None

    def set_last(self, name: str | None) -> None:
        path = self.base / "last-profile"
        if name:
            self._mkdir(self.base)
            atomic_write(path, name.encode("utf-8"))
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    # ---- lifecycle ----------------------------------------------------------
    def remove(self, name: str) -> None:
        shutil.rmtree(self._dir(name), ignore_errors=True)
        if self.last() is None:
            self.set_last(None)

    def discard_if_empty(self, name: str) -> None:
        """Drop a profile that never got a token (e.g. an abandoned pairing)."""
        if name in self.names() and not self.has_token(name) and not self.has_pending(name):
            self.remove(name)

    def migrate_legacy(self) -> str | None:
        """Move <base>/device-token, state.json, pending-action.json into a profile. Returns its name."""
        present = [f for f in LEGACY_FILES if (self.base / f).is_file()]
        if not present:
            return None
        name = self.unique_name("default")
        self.cache_for(name)
        for f in present:
            os.replace(self.base / f, self.root / name / f)
        if not self.last():
            self.set_last(name)
        return name
