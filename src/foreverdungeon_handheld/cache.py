"""Atomic local cache: device token (0600), last state snapshot, one pending action envelope.

Nothing here is authoritative. The cache only lets the device show the last
server-provided content while offline and resend one uncertain action.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


def atomic_write(path: str | os.PathLike, data: bytes, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Cache:
    def __init__(self, state_path, token_path, pending_path=None, max_recent_events: int = 30):
        self.state_path = Path(state_path)
        self.token_path = Path(token_path)
        self.pending_path = Path(pending_path) if pending_path else self.state_path.with_name("pending-action.json")
        self.max_recent_events = max_recent_events
        self.recovered_corrupt: list[str] = []

    # ---- json helpers -------------------------------------------------
    def _read_json(self, path: Path):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data
        except (ValueError, UnicodeDecodeError):
            # Corrupt file: move aside (never silently trust it) and continue empty.
            aside = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            try:
                os.replace(path, aside)
            except OSError:
                pass
            self.recovered_corrupt.append(str(path))
            return None

    def _write_json(self, path: Path, data: dict) -> None:
        atomic_write(path, json.dumps(data, separators=(",", ":")).encode("utf-8"))

    # ---- token --------------------------------------------------------
    def load_token(self) -> dict | None:
        data = self._read_json(self.token_path)
        if data and data.get("access_token") and data.get("character_id"):
            return data
        return None

    def save_token(self, access_token: str, character_id: str) -> None:
        self._write_json(self.token_path, {"access_token": access_token, "character_id": character_id})

    def clear_token(self) -> None:
        try:
            self.token_path.unlink()
        except FileNotFoundError:
            pass

    # ---- state snapshot -----------------------------------------------
    def load_state(self) -> dict | None:
        return self._read_json(self.state_path)

    def save_state(self, snapshot: dict) -> None:
        snap = dict(snapshot)
        if isinstance(snap.get("recent"), list):
            snap["recent"] = snap["recent"][-self.max_recent_events:]
        self._write_json(self.state_path, snap)

    # ---- pending envelope (at most one) ---------------------------------
    def load_pending(self) -> dict | None:
        data = self._read_json(self.pending_path)
        if data and data.get("command") and data.get("idempotency_key") and data.get("character_id"):
            return data
        return None

    def save_pending(self, envelope: dict) -> None:
        self._write_json(self.pending_path, envelope)

    def clear_pending(self) -> None:
        try:
            self.pending_path.unlink()
        except FileNotFoundError:
            pass
