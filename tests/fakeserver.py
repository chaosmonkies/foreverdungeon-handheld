"""In-process fake of the Forever Dungeon API (shapes copied from api/app/main.py + game.py)."""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class World:
    def __init__(self, poll_interval=1):
        self.lock = threading.Lock()
        self.poll_interval = poll_interval
        self.pairings: dict = {}       # device_code -> dict(status, user_code)
        self.tokens: dict = {}         # token -> character_id
        self.revoked: set = set()
        self.revision = 3
        self.gold = 10
        self.location = "loc_tavern"
        self.events: list = []
        self.idem: dict = {}           # key -> (command, response)
        self.faults: dict = {"state": [], "action": [], "token": [], "code": []}
        self.action_posts = 0
        self.last_auth = None
        self.description = "Rain marks the tavern windows. A hearth crackles."
        self.provision_keys = {"fake-provision-key"}
        self.accounts: dict = {}       # username -> {email, password, characters: [{id, name, ...}]}
        self.account_posts: list = []  # (path, body) for assertions
        self.device_tokens: dict = {}  # token -> username

    # -- helpers used by tests ------------------------------------------------
    def approve(self, user_code=None, character_id="chr_test"):
        for p in self.pairings.values():
            if user_code is None or p["user_code"] == user_code:
                p["status"] = "approved"
                p["character_id"] = character_id

    def expire_all(self):
        for p in self.pairings.values():
            p["status"] = "expired"

    def choices(self):
        out = [{"label": "Look around", "command": "look"},
               {"label": "Talk to Mira the barkeep", "command": "talk to Mira"},
               {"label": "Take copper coin", "command": "take copper coin"},
               {"label": "Go east: Muddy road", "command": "go east"},
               {"label": "Explore a new way", "command": "explore north"},
               {"label": "Check inventory", "command": "inventory"},
               {"label": "Rest by the hearth", "command": "rest"}]
        return out

    def state(self, cid):
        name = "The Lantern Tavern" if self.location == "loc_tavern" else "Muddy Road"
        return {
            "character": {"id": cid, "name": "Wren", "hp": 12, "max_hp": 14, "level": 1, "xp": 0, "gold": self.gold,
                          "statuses": [], "revision": self.revision, "location_id": self.location},
            "location": {"id": self.location, "name": name, "summary": self.description, "description": self.description,
                         "expanded_description": self.description + " Old maps cover one wall.", "kind": "tavern",
                         "weather": "rain", "revision": 1,
                         "exits": [{"id": "ex1", "direction": "east", "label": "Muddy road", "to_location_id": "loc_road", "travel_minutes": 5}],
                         "npcs": [{"id": "npc1", "name": "Mira", "description": "Barkeep", "disposition": "friendly"}],
                         "items": [{"id": "it1", "name": "copper coin", "description": "Dull.", "condition": "worn"}],
                         "players": []},
            "inventory": [{"id": "i1", "name": "Torch", "description": "Pitch-soaked.", "kind": "tool", "quantity": 2,
                           "weight": 1, "condition": "good", "equipped_slot": None, "value": 1},
                          {"id": "i2", "name": "Short sword", "description": "Notched.", "kind": "weapon", "quantity": 1,
                           "weight": 3, "condition": "good", "equipped_slot": "main_hand", "value": 8}],
            "carrying": {"current": 5, "maximum": 40},
            "journal": [{"id": "q1", "title": "The missing ledger", "summary": "Mira lost her ledger.", "status": "active",
                         "objectives": [{"text": "Ask around the tavern"}]}],
            "relationships": [{"target_id": "npc1", "name": "Mira", "affinity": 1, "trust": 0, "notes": ""}],
            "discoveries": [{"id": "loc_tavern", "name": "The Lantern Tavern", "summary": "Warm.", "discovered_at": "2026-10-01T00:00:00"}],
            "events": [{"id": e["id"], "type": "action", "summary": e["summary"], "created_at": "2026-10-01T00:00:00"} for e in self.events[-20:]],
            "party_id": None, "encounter": None, "world_time": {"day": 3, "hour": 19},
            "choices": self.choices(),
        }


def make_handler(world: World):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, status, body=None, raw=None, headers=None):
            data = raw if raw is not None else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _fault(self, kind):
            with world.lock:
                return world.faults[kind].pop(0) if world.faults[kind] else None

        def _common_fault(self, f):
            if f == 429:
                self._send(429, {"detail": "Too many requests"}, headers={"Retry-After": "2"})
                return True
            if f == 503:
                self._send(503, {"detail": "Service unavailable"})
                return True
            if f == "malformed":
                self._send(200, raw=b"{not json")
                return True
            return False

        def _auth(self):
            h = self.headers.get("Authorization", "")
            world.last_auth = h
            tok = h[7:] if h.startswith("Bearer ") else None
            if not tok or tok not in world.tokens or tok in world.revoked:
                self._send(401, {"detail": "Invalid or revoked device token"})
                return None
            return world.tokens[tok]

        def do_GET(self):
            if self.path.startswith("/api/game/") and self.path.endswith("/state"):
                f = self._fault("state")
                if self._common_fault(f):
                    return
                cid = self._auth()
                if cid is None:
                    return
                with world.lock:
                    self._send(200, world.state(cid))
                return
            self._send(404, {"detail": "Not found"})

        def do_POST(self):
            body = self._body()
            if self.path == "/api/devices/code":
                f = self._fault("code")
                if self._common_fault(f):
                    return
                dc = secrets.token_urlsafe(30)
                uc = "ABCDE-" + str(23456 + len(world.pairings))
                world.pairings[dc] = {"status": "pending", "user_code": uc, "device_name": body.get("device_name")}
                self._send(201, {"device_code": dc, "user_code": uc,
                                 "verification_url": f"https://foreverdungeon.moonshinelabs.io/?pair={uc}",
                                 "expires_in": 600, "poll_interval": world.poll_interval})
                return
            if self.path == "/api/devices/token":
                f = self._fault("token")
                if self._common_fault(f):
                    return
                p = world.pairings.get(body.get("device_code"))
                if not p:
                    return self._send(404, {"detail": "Unknown device code"})
                if p["status"] == "expired":
                    return self._send(410, {"detail": "Pairing code expired"})
                if p["status"] == "pending":
                    return self._send(202, {"status": "authorization_pending"})
                if p["status"] == "consumed":
                    return self._send(410, {"detail": "Pairing code was already used"})
                tok = secrets.token_urlsafe(30)
                world.tokens[tok] = p["character_id"]
                p["status"] = "consumed"
                return self._send(200, {"status": "approved", "access_token": tok, "token_type": "Bearer",
                                        "character_id": p["character_id"]})
            if self.path in ("/api/devices/signup", "/api/devices/login"):
                return self._account(body)
            if self.path.startswith("/api/game/") and self.path.endswith("/actions"):
                f = self._fault("action")
                if self._common_fault(f):
                    return
                cid = self._auth()
                if cid is None:
                    return
                with world.lock:
                    world.action_posts += 1
                    key, cmd = body["idempotency_key"], body["command"]
                    if key in world.idem:
                        old_cmd, resp = world.idem[key]
                        if old_cmd != cmd:
                            return self._send(409, {"detail": "Idempotency key was already used for another action"})
                        return self._send(200, dict(resp, idempotent_replay=True))
                    if body.get("expected_revision") is not None and body["expected_revision"] != world.revision:
                        return self._send(409, {"detail": {"message": "Character state changed; refresh before retrying",
                                                           "current_revision": world.revision}})
                    if cmd.startswith("take"):
                        world.gold += 1
                        world.revision += 1
                        narration = "You pocket the copper coin."
                    elif cmd == "go east":
                        world.location = "loc_road"
                        world.revision += 1
                        narration = "You step onto the muddy road."
                    else:
                        narration = f"You {cmd}. The tavern hums around you."
                    ev = {"id": f"evt_{len(world.events) + 1}", "summary": narration}
                    world.events.append(ev)
                    resp = {"narration": narration, "event": {"id": ev["id"], "type": "action", "summary": narration},
                            "character_revision": world.revision, "generated_with_llm": False, "choices": world.choices()}
                    world.idem[key] = (cmd, resp)
                if f == "drop_after_commit":
                    # committed server-side, but the socket dies before the response is written
                    self.close_connection = True
                    try:
                        self.connection.shutdown(2)
                    except OSError:
                        pass
                    return
                return self._send(200, resp)
            self._send(404, {"detail": "Not found"})

        def _issue(self, username, character):
            tok = secrets.token_urlsafe(30)
            world.tokens[tok] = character["id"]
            world.device_tokens[tok] = username
            chars = world.accounts[username]["characters"]
            return {"status": "approved", "access_token": tok, "token_type": "Bearer", "character_id": character["id"],
                    "device_id": "dev_" + tok[:6], "username": username, "character": character, "characters": chars}

        def _account(self, body):
            with world.lock:
                world.account_posts.append((self.path, dict(body)))
                if not world.provision_keys:
                    return self._send(404, {"detail": "Not Found"})
                if body.get("provision_key") not in world.provision_keys:
                    return self._send(403, {"detail": "This device is not authorized to create or sign in to accounts"})
                if self.path.endswith("/signup"):
                    username = body.get("username", "")
                    if len(body.get("password", "")) < 10:
                        return self._send(422, {"detail": [{"loc": ["body", "password"], "msg": "String should have at least 10 characters"}]})
                    if any(u.casefold() == username.casefold() or a["email"] == body.get("email") for u, a in world.accounts.items()):
                        return self._send(409, {"detail": "Email or username is already registered"})
                    ch = {"id": f"chr_{username}", "name": body.get("character_name") or username.title(), "background": "wayfarer", "level": 1}
                    world.accounts[username] = {"email": body["email"], "password": body["password"], "characters": [ch]}
                    return self._send(201, self._issue(username, ch))
                login = body.get("login", "").casefold()
                user = next((u for u, a in world.accounts.items() if login in (u.casefold(), a["email"].casefold())), None)
                if not user or world.accounts[user]["password"] != body.get("password"):
                    return self._send(401, {"detail": "Invalid credentials"})
                chars = world.accounts[user]["characters"]
                if body.get("character_id"):
                    ch = next((c for c in chars if c["id"] == body["character_id"]), None)
                    if not ch:
                        return self._send(404, {"detail": "Character not found on this account"})
                elif len(chars) == 1:
                    ch = chars[0]
                else:
                    return self._send(200, {"status": "choose_character", "username": user, "characters": chars})
                return self._send(200, self._issue(user, ch))

    return H


class FakeServer:
    def __init__(self, poll_interval=1):
        self.world = World(poll_interval)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.world))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
