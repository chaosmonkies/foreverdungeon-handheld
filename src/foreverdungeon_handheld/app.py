"""Client state machine. No display-specific code and no network on the caller's thread.

PICKER (profiles) -> FORM (sign up / sign in) -> AUTH_PENDING [-> CHOOSE_CHAR] -> SYNCING
PICKER -> UNPAIRED -> SHOW_CODE (polling) -> SYNCING -> PLAY (OFFLINE flag = cached view)
PICKER -> existing profile -> SYNCING / PLAY / ACTION_PENDING
PLAY -> ACTION_PENDING -> PLAY
PLAY -> MENU_INVENTORY / MENU_JOURNAL / MENU_WORLD / COMMAND_ENTRY

The UI thread calls `handle_key()`, `tick()` and `screen()`. Every network call
runs on a single background worker; results are applied in `tick()`. The
server is authoritative: the app only shows server text and server-provided
choices, persists one action envelope, and resends it verbatim when uncertain.
"""
from __future__ import annotations

import enum
import queue
import threading
import time
import urllib.parse

from . import api as fdapi
from .api import new_envelope
from .input import Action, CHAR_SHORTCUTS, translate
from .layout import Row, Screen, first_item_on, page_of_item, paginate


class S(enum.Enum):
    PICKER = "picker"
    FORM = "form"
    AUTH_PENDING = "auth_pending"
    CHOOSE_CHAR = "choose_char"
    CONFIRM_REMOVE = "confirm_remove"
    UNPAIRED = "unpaired"
    SHOW_CODE = "show_code"
    EXPIRED = "expired"
    SYNCING = "syncing"
    PLAY = "play"
    ACTION_PENDING = "action_pending"
    MENU_INVENTORY = "menu_inventory"
    MENU_JOURNAL = "menu_journal"
    MENU_WORLD = "menu_world"
    COMMAND_ENTRY = "command_entry"
    REVOKED = "revoked"


MENUS = [S.MENU_INVENTORY, S.MENU_JOURNAL, S.MENU_WORLD]
ACCOUNT_STATES = (S.PICKER, S.FORM, S.AUTH_PENDING, S.CHOOSE_CHAR, S.CONFIRM_REMOVE)

# (key, label, masked, optional, hint)
SIGNUP_FIELDS = [
    ("username", "Username", False, False, "3-32 letters, digits, _ or -"),
    ("email", "Email", False, False, "Used for account recovery"),
    ("password", "Password", True, False, "At least 10 characters"),
    ("confirm", "Repeat password", True, False, "Type the password again"),
    ("character_name", "Character name", False, True, "Optional. Blank = from username"),
]
SIGNIN_FIELDS = [
    ("login", "Username or email", False, False, "Your Forever Dungeon account"),
    ("password", "Password", True, False, ""),
]
PICKER_ACTIONS = [("new", "+ New account"), ("signin", "+ Sign in"), ("pair", "+ Pair code")]


class Worker:
    """One background thread, one job at a time, results handed back via a queue."""

    def __init__(self, threaded: bool = True):
        self.threaded = threaded
        self.jobs: queue.Queue = queue.Queue()
        self.results: queue.Queue = queue.Queue()
        self._sync: list = []
        self._thread = None
        if threaded:
            self._thread = threading.Thread(target=self._loop, name="fdh-net", daemon=True)
            self._thread.start()

    def submit(self, tag: str, fn, *args):
        if self.threaded:
            self.jobs.put((tag, fn, args))
        else:
            self._sync.append((tag, fn, args))

    @staticmethod
    def _run(tag, fn, args):
        try:
            return tag, fn(*args), None
        except Exception as exc:  # classified by the app
            return tag, None, exc

    def _loop(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            self.results.put(self._run(*job))

    def drain(self) -> list:
        if not self.threaded:
            out = [self._run(*j) for j in self._sync]
            self._sync = []
            return out
        out = []
        while True:
            try:
                out.append(self.results.get_nowait())
            except queue.Empty:
                return out

    def stop(self):
        if self.threaded:
            self.jobs.put(None)


def _short_url(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc or url
    except Exception:
        return url


class App:
    def __init__(self, api: fdapi.ApiClient, cache, *, width: int, height: int, device_name: str = "E-paper handheld",
                 text_entry: bool = True, state_poll_seconds: float = 30.0, clock=time.monotonic,
                 threaded: bool = True, profiles=None, provision_key=None):
        self.api = api
        self.cache = cache
        self.profiles = profiles            # ProfileStore or None (single-account mode)
        self.provision_key = provision_key  # callable -> str | None; the key is never stored on self
        self.profile: str | None = None
        self.epoch = 0                      # bumped on profile switch; stale worker results are dropped
        self.form: dict | None = None
        self.account_choices: list = []
        self.remove_target: str | None = None
        self.w, self.h = width, height
        self.device_name = device_name
        self.text_entry = text_entry
        self.state_poll_seconds = state_poll_seconds
        self.clock = clock
        self.worker = Worker(threaded)
        self.state = S.UNPAIRED
        self.inflight: set[str] = set()
        self.character_id: str | None = None
        self.snapshot: dict = {}
        self.pending: dict | None = None
        self.pairing: dict | None = None       # in memory only; device_code never displayed/logged
        self.code_expires_at = 0.0
        self.next_poll_at = 0.0
        self.next_state_at = 0.0
        self.next_action_retry_at = 0.0
        self.next_code_at = 0.0
        self.backoff = 0
        self.offline = False
        self.message = ""
        self.cursor = 0
        self.page = 0
        self.buffer = ""
        self.expanded: set = set()
        self.return_state = S.PLAY
        self.quit = False
        self.manual_refresh = 0  # bumped to request a full refresh

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self.profiles is not None:
            self.profiles.migrate_legacy()
            self._show_picker()
            return
        self._start_session()

    def _start_session(self):
        tok = self.cache.load_token()
        snap = self.cache.load_state()
        if tok:
            self.api.token = tok["access_token"]
            self.character_id = tok["character_id"]
            if snap and snap.get("character_id") == self.character_id:
                self.snapshot = snap
            self.pending = self.cache.load_pending()
            if self.pending and self.pending.get("character_id") != self.character_id:
                self.cache.clear_pending()
                self.pending = None
            self.state = S.PLAY if self.snapshot.get("state") else S.SYNCING
            if self.pending:
                self.state = S.ACTION_PENDING
                self._send_pending()
            else:
                self._fetch_state()
        else:
            if snap:
                self.snapshot = snap  # readable offline cache survives unpairing
            self._request_code()
        if self.cache.recovered_corrupt:
            self.message = "Local cache was corrupt and was reset."

    def stop(self):
        self.worker.stop()

    @property
    def busy(self) -> bool:
        return bool(self.inflight)

    def _submit(self, tag, fn, *args):
        if tag in self.inflight:
            return
        self.inflight.add(tag)
        self.worker.submit((tag, self.epoch), fn, *args)

    def _delay(self, exc=None) -> float:
        self.backoff = min(self.backoff + 1, 6)
        base = min(60.0, 2.0 ** self.backoff)
        ra = getattr(exc, "retry_after", None)
        return max(base, float(ra)) if ra else base

    # ------------------------------------------------------------------ jobs
    def _request_code(self):
        self.state = S.UNPAIRED
        self._submit("code", self.api.request_code, self.device_name)

    def _poll(self):
        if self.pairing:
            self._submit("poll", self.api.poll_token, self.pairing["device_code"])

    def _fetch_state(self):
        if self.character_id:
            self._submit("state", self.api.get_state, self.character_id)

    def _send_pending(self):
        if self.pending:
            self._submit("action", self.api.send_action, dict(self.pending))

    # ------------------------------------------------------------------ tick
    def tick(self):
        for (tag, epoch), result, exc in self.worker.drain():
            if epoch != self.epoch:
                continue  # belongs to a profile we already left; its envelope stays on disk
            self.inflight.discard(tag)
            getattr(self, f"_on_{tag}")(result, exc)
        now = self.clock()
        if self.state == S.SHOW_CODE:
            if now >= self.code_expires_at:
                self.pairing = None
                self.state = S.EXPIRED
            elif now >= self.next_poll_at and "poll" not in self.inflight:
                self._poll()
        elif self.state == S.UNPAIRED and not self.inflight and now >= self.next_code_at and self.next_code_at:
            self._request_code()
        elif self.character_id and self.api.token and self.state not in (S.REVOKED, S.UNPAIRED, S.SHOW_CODE, S.EXPIRED):
            if self.pending:
                if "action" not in self.inflight and now >= self.next_action_retry_at:
                    self._send_pending()
            elif "state" not in self.inflight and now >= self.next_state_at:
                self._fetch_state()

    # ------------------------------------------------------------------ results
    def _on_code(self, result, exc):
        if exc:
            self.offline = isinstance(exc, (fdapi.NetworkError, fdapi.ServerUnavailable, fdapi.MalformedResponse))
            self.message = "Can't get a pairing code: " + str(exc)
            self.next_code_at = self.clock() + self._delay(exc)
            self.state = S.UNPAIRED
            return
        self.backoff = 0
        self.offline = False
        self.message = ""
        self.pairing = result
        now = self.clock()  # monotonic: immune to wall-clock skew
        self.code_expires_at = now + float(result.get("expires_in") or 600)
        self.poll_interval = max(1.0, float(result.get("poll_interval") or 5))
        self.next_poll_at = now + self.poll_interval
        self.state = S.SHOW_CODE

    def _on_poll(self, result, exc):
        now = self.clock()
        if exc:
            if isinstance(exc, fdapi.Expired):
                self.pairing = None
                self.state = S.EXPIRED
                return
            if isinstance(exc, fdapi.ApiError) and exc.status == 404:
                self.pairing = None
                self.state = S.EXPIRED
                return
            self.offline = isinstance(exc, (fdapi.NetworkError, fdapi.ServerUnavailable))
            wait = self.poll_interval * 2
            if getattr(exc, "retry_after", None):
                wait = max(wait, float(exc.retry_after))
            self.next_poll_at = now + min(60.0, wait)
            return
        self.offline = False
        if result.get("status") == "approved":
            self.cache.save_token(result["access_token"], result["character_id"])
            self.api.token = result["access_token"]
            self.character_id = result["character_id"]
            self.pairing = None  # erase device code
            if self.profiles is not None and self.profile:
                self.profiles.set_last(self.profile)
            if self.pending and self.pending.get("character_id") != self.character_id:
                self.cache.clear_pending()
                self.pending = None
            if self.snapshot.get("character_id") != self.character_id:
                self.snapshot = {}
            self.state = S.SYNCING
            self.message = ""
            self.next_state_at = 0.0
            self._fetch_state()
        else:
            self.next_poll_at = now + self.poll_interval

    def _on_state(self, result, exc):
        now = self.clock()
        if exc:
            if isinstance(exc, fdapi.Unauthorized):
                return self._revoked()
            self.offline = True
            if isinstance(exc, fdapi.ApiError) and exc.status in (403, 404):
                self.message = str(exc)
            self.next_state_at = now + self._delay(exc)
            return
        self.backoff = 0
        self.offline = False
        old_loc = (self.snapshot.get("state") or {}).get("location", {}).get("id")
        self.snapshot["character_id"] = self.character_id
        self.snapshot["state"] = result
        if self.profiles is not None and self.profile:
            self.profiles.update_meta(self.profile, character_name=(result.get("character") or {}).get("name"))
        self.snapshot.pop("response_choices", None)
        self.cache.save_state(self.snapshot)
        if old_loc != result["location"].get("id"):
            self.cursor = self.page = 0
            self.expanded.clear()
        if self.state == S.SYNCING:
            self.state = S.PLAY
            self.cursor = self.page = 0
        self._clamp_cursor()
        self.next_state_at = now + self.state_poll_seconds

    def _on_action(self, result, exc):
        now = self.clock()
        if exc:
            if isinstance(exc, fdapi.Unauthorized):
                # Keep the envelope: the server may already have committed; we can't know.
                return self._revoked()
            if isinstance(exc, fdapi.Conflict) and exc.in_progress:
                self.next_action_retry_at = now + self._delay(exc)
                return
            if isinstance(exc, (fdapi.NetworkError, fdapi.MalformedResponse, fdapi.RateLimited, fdapi.ServerUnavailable)):
                # Uncertain or transient: resend the SAME envelope later. Never mint a new key.
                self.offline = isinstance(exc, (fdapi.NetworkError, fdapi.ServerUnavailable))
                self.message = "Action pending: " + str(exc)
                self.next_action_retry_at = now + self._delay(exc)
                return
            # Definitive rejection (409 conflict, 4xx): drop the envelope and resync.
            self.cache.clear_pending()
            self.pending = None
            if isinstance(exc, fdapi.Conflict) and exc.current_revision is not None:
                self.message = "The world changed. Choose again."
            else:
                self.message = str(exc)
            self.state = S.PLAY
            self.cursor = self.page = 0
            self.next_state_at = 0.0
            self._fetch_state()
            return
        self.backoff = 0
        self.offline = False
        recent = list(self.snapshot.get("recent") or [])
        narration = str(result.get("narration") or "")
        recent.append({"command": self.pending["command"] if self.pending else "", "narration": narration})
        self.snapshot["recent"] = recent
        self.snapshot["last_narration"] = narration
        self.snapshot["response_choices"] = result.get("choices") or []
        self.snapshot["character_id"] = self.character_id
        self.cache.save_state(self.snapshot)   # durably cached first ...
        self.cache.clear_pending()             # ... then the envelope may go
        self.pending = None
        self.message = ""
        self.state = S.PLAY
        self.cursor = self.page = 0
        self.next_state_at = 0.0
        self._fetch_state()

    def _revoked(self):
        self.cache.clear_token()  # cache file with readable text is kept
        self.api.token = None
        self.state = S.REVOKED
        self.message = "This device was unpaired or its token is no longer valid."

    # ------------------------------------------------------------------ profiles / accounts
    def _reset_session(self):
        self.epoch += 1
        self.inflight.clear()
        self.api.token = None
        self.character_id = None
        self.snapshot = {}
        self.pending = None
        self.pairing = None
        self.offline = False
        self.backoff = 0
        self.message = ""
        self.buffer = ""
        self.cursor = self.page = 0
        self.expanded.clear()
        self.next_poll_at = self.next_state_at = self.next_action_retry_at = self.next_code_at = 0.0

    def picker_entries(self) -> list[tuple]:
        out = []
        for name in self.profiles.names() if self.profiles is not None else []:
            meta = self.profiles.meta(name)
            label = meta.get("username") or name
            if meta.get("character_name"):
                label += f" ({meta['character_name']})"
            if not self.profiles.has_token(name):
                label += " [signed out]"
            if self.profiles.has_pending(name):
                label += " [1 pending]"
            out.append(("profile", name, label))
        return out + [(kind, None, label) for kind, label in PICKER_ACTIONS]

    def _show_picker(self, message: str = ""):
        if self.profile and self.profiles is not None:
            self.profiles.discard_if_empty(self.profile)
        self._reset_session()
        self.profile = None
        self.cache = None
        self.form = None
        self.account_choices = []
        self.state = S.PICKER
        self.message = message
        last = self.profiles.last()
        entries = self.picker_entries()
        self.cursor = next((i for i, e in enumerate(entries) if e[0] == "profile" and e[1] == last), 0)

    def _open_profile(self, name: str):
        self._reset_session()
        self.profile = name
        self.cache = self.profiles.cache_for(name)
        self.profiles.set_last(name)
        self._start_session()

    def _picker_select(self):
        entries = self.picker_entries()
        if not 0 <= self.cursor < len(entries):
            return
        kind, name, _ = entries[self.cursor]
        if kind == "profile":
            return self._open_profile(name)
        if kind == "pair":
            self._reset_session()
            self.profile = self.profiles.unique_name("paired")
            self.cache = self.profiles.cache_for(self.profile)
            return self._request_code()
        fields = SIGNUP_FIELDS if kind == "new" else SIGNIN_FIELDS
        self.form = {"kind": "signup" if kind == "new" else "signin", "fields": fields, "index": 0, "values": {}}
        self.buffer = ""
        self.message = ""
        self.state = S.FORM

    def _form_field(self):
        return self.form["fields"][self.form["index"]]

    def _form_goto(self, index: int, message: str = ""):
        self.form["index"] = max(0, min(index, len(self.form["fields"]) - 1))
        key, _, masked, _, _ = self._form_field()
        # Masked fields always restart empty; others keep what was typed.
        self.buffer = "" if masked else self.form["values"].get(key, "")
        self.message = message
        self.state = S.FORM

    def _form_next(self):
        key, label, masked, optional, _ = self._form_field()
        value = self.buffer if masked else self.buffer.strip()
        if not value and not optional:
            self.message = f"{label} is required."
            return
        values = self.form["values"]
        if self.form["kind"] == "signup":
            if key == "username" and not (3 <= len(value) <= 32):
                self.message = "Username must be 3-32 characters."
                return
            if key == "email" and ("@" not in value or "." not in value.rsplit("@", 1)[-1]):
                self.message = "That doesn't look like an email address."
                return
            if key == "password" and len(value) < 10:
                self.message = "Password must be at least 10 characters."
                return
            if key == "confirm" and value != values.get("password"):
                values.pop("password", None)
                return self._form_goto(self._field_index("password"), "Passwords didn't match. Enter it again.")
        values[key] = value
        if self.form["index"] + 1 < len(self.form["fields"]):
            return self._form_goto(self.form["index"] + 1)
        self._submit_account()

    def _field_index(self, key: str) -> int:
        return next((i for i, f in enumerate(self.form["fields"]) if f[0] == key), len(self.form["fields"]) - 1)

    def _submit_account(self, character_id: str | None = None):
        key = self.provision_key() if self.provision_key else None
        if not key:
            self.message = "This device has no provision key. Use '+ Pair code' instead."
            self.state = S.FORM
            return
        v = self.form["values"]
        if self.form["kind"] == "signup":
            job = lambda: self.api.device_signup(  # noqa: E731
                email=v["email"], username=v["username"], password=v["password"], provision_key=key,
                device_name=self.device_name, character_name=v.get("character_name") or None)
        else:
            job = lambda: self.api.device_login(  # noqa: E731
                login=v["login"], password=v["password"], provision_key=key,
                device_name=self.device_name, character_id=character_id)
        self.message = ""
        self.state = S.AUTH_PENDING
        self._submit("auth", job)

    def _on_auth(self, result, exc):
        if exc:
            return self._auth_failed(exc)
        if result.get("status") == "choose_character":
            self.account_choices = list(result.get("characters") or [])
            self.form["username"] = result.get("username")
            self.state = S.CHOOSE_CHAR
            self.cursor = self.page = 0
            self.message = ""
            return
        username = result.get("username") or self.form["values"].get("username") or self.form["values"].get("login", "")
        name = self.profiles.name_for_username(username)
        self.form = None  # drops the password from memory
        self._reset_session()
        self.profile = name
        self.cache = self.profiles.cache_for(name)
        self.cache.clear_pending()  # a fresh token may be bound to another character
        self.cache.save_token(result["access_token"], result["character_id"])
        self.profiles.update_meta(name, username=username, character_name=(result.get("character") or {}).get("name"))
        self.profiles.set_last(name)
        self._start_session()

    def _auth_failed(self, exc):
        status = getattr(exc, "status", None)
        detail = str(exc)
        signup = self.form["kind"] == "signup"
        if isinstance(exc, fdapi.NetworkError) or isinstance(exc, fdapi.ServerUnavailable):
            return self._form_goto(len(self.form["fields"]) - 1, "Can't reach the server. Try again.")
        if isinstance(exc, fdapi.RateLimited):
            return self._form_goto(len(self.form["fields"]) - 1, "Too many attempts. Wait a while and try again.")
        if status == 404 and "character" not in detail.lower():
            return self._form_goto(0, "Device sign-up is turned off on this server. Use '+ Pair code'.")
        if status == 403:
            return self._form_goto(0, detail or "This device is not authorized.")
        if isinstance(exc, fdapi.Unauthorized):
            self.form["values"].pop("password", None)
            return self._form_goto(self._field_index("password"), "Wrong username/email or password.")
        low = detail.lower()
        if signup and status == 409:
            return self._form_goto(self._field_index("username"), detail or "Username or email already registered.")
        for key in ("username", "email", "password", "character_name"):
            if key.replace("_", " ") in low or key in low:
                if key == "password":
                    self.form["values"].pop("password", None)
                return self._form_goto(self._field_index(key), detail)
        return self._form_goto(len(self.form["fields"]) - 1, detail or "Request failed.")

    def _handle_account_key(self, action, ch):
        if self.state == S.FORM:
            if action == Action.SELECT:
                return self._form_next()
            if action == Action.BACK:
                if self.form["index"] == 0:
                    return self._show_picker()
                return self._form_goto(self.form["index"] - 1)
            if action == Action.BACKSPACE:
                self.buffer = self.buffer[:-1]
            elif action == Action.CHAR and ch and ch.isprintable() and len(self.buffer) < 256:
                self.buffer += ch
            return
        if self.state == S.AUTH_PENDING:
            return  # wait; the request cannot be cancelled half-way
        if self.state == S.CONFIRM_REMOVE:
            if action == Action.SELECT and self.remove_target:
                self.profiles.remove(self.remove_target)
                gone, self.remove_target = self.remove_target, None
                return self._show_picker(f"Removed {gone} from this device.")
            if action in (Action.BACK, Action.BACKSPACE):
                self.remove_target = None
                return self._show_picker()
            return
        if action == Action.CHAR and ch:
            action = CHAR_SHORTCUTS.get(ch.lower(), Action.CHAR)
            if self.state == S.PICKER and ch.lower() in ("x", "d"):
                entries = self.picker_entries()
                if 0 <= self.cursor < len(entries) and entries[self.cursor][0] == "profile":
                    self.remove_target = entries[self.cursor][1]
                    self.state = S.CONFIRM_REMOVE
                return
        if action == Action.UP:
            return self._move(-1)
        if action == Action.DOWN:
            return self._move(1)
        if action in (Action.LEFT, Action.RIGHT):
            return self._flip(-1 if action == Action.LEFT else 1)
        if self.state == S.CHOOSE_CHAR:
            if action == Action.SELECT and 0 <= self.cursor < len(self.account_choices):
                return self._submit_account(character_id=self.account_choices[self.cursor]["id"])
            if action in (Action.BACK, Action.BACKSPACE):
                return self._show_picker()
            return
        # PICKER
        if action == Action.SELECT:
            return self._picker_select()
        if action in (Action.BACK, Action.QUIT):
            self.quit = True

    def _account_screen(self) -> Screen:
        msg = [Row("text", "! " + self.message), Row("gap")] if self.message else []
        if self.state == S.PICKER:
            rows = list(msg)
            if not any(e[0] == "profile" for e in self.picker_entries()):
                rows.append(Row("text", "No accounts on this device yet."))
            rows += [Row("item", label, (kind, name)) for kind, name, label in self.picker_entries()]
            return Screen(("picker",), "Forever Dungeon", "", rows, cursor=self.cursor, page=self.page,
                          footer="Enter:open x:remove Esc:exit")
        if self.state == S.CONFIRM_REMOVE:
            name = self.remove_target or ""
            rows = [Row("text", f"Remove '{name}' from this device?")]
            if self.profiles.has_pending(name):
                rows.append(Row("text", "Its 1 unsent action will be lost."))
            rows.append(Row("text", "The account stays on the server; revoke the device on the website."))
            return Screen(("remove", name), "Remove account", "", rows, footer="Enter:remove Esc:keep")
        title = "New account" if self.form and self.form["kind"] == "signup" else "Sign in"
        if self.state == S.AUTH_PENDING:
            return Screen(("auth",), title, "", [Row("text", "Contacting the server...")], footer="Please wait")
        if self.state == S.CHOOSE_CHAR:
            rows = msg + [Row("heading", "Choose a character")]
            rows += [Row("item", f"{c.get('name', '?')} (lvl {c.get('level', '?')} {c.get('background', '')})".replace(" )", ")"))
                     for c in self.account_choices]
            return Screen(("choose",), title, "", rows, cursor=self.cursor, page=self.page, footer="Enter:play Esc:cancel")
        key, label, masked, optional, hint = self._form_field()
        n, i = len(self.form["fields"]), self.form["index"] + 1
        rows = msg + [Row("heading", label)]
        if hint:
            rows.append(Row("text", hint))
        shown = "*" * len(self.buffer) if masked else self.buffer
        return Screen(("form", self.form["kind"], i), f"{title} {i}/{n}", "", rows,
                      footer="Enter:next Esc:back", input_line=shown)

    # ------------------------------------------------------------------ actions
    def choices(self) -> list[dict]:
        if self.snapshot.get("response_choices"):
            return self.snapshot["response_choices"]
        return list((self.snapshot.get("state") or {}).get("choices") or [])

    def submit_command(self, command: str):
        command = command.strip()[:500]
        if not command or not self.character_id or self.state == S.REVOKED:
            return
        if self.pending:
            self.message = "One action is already pending."
            return
        revision = (self.snapshot.get("state") or {}).get("character", {}).get("revision")
        env = new_envelope(self.character_id, command, revision)
        self.cache.save_pending(env)  # persisted BEFORE it is sent
        self.pending = env
        self.message = ""
        self.state = S.ACTION_PENDING
        self.next_action_retry_at = 0.0
        self._send_pending()

    def _pages(self):
        return paginate(self.screen(), self.w, self.h)

    def _item_count(self) -> int:
        return sum(1 for r in self.screen().rows if r.kind == "item")

    def _clamp_cursor(self):
        n = self._item_count()
        self.cursor = max(0, min(self.cursor, max(0, n - 1)))

    def _move(self, delta: int):
        n = self._item_count()
        pages = self._pages()
        if n == 0:
            self.page = max(0, min(len(pages) - 1, self.page + delta))
            return
        self.cursor = (self.cursor + delta) % n
        self.page = page_of_item(pages, self.cursor)

    def _flip(self, delta: int):
        pages = self._pages()
        self.page = max(0, min(len(pages) - 1, self.page + delta))
        first = first_item_on(pages, self.page)
        if first is not None:
            self.cursor = first

    def _enter(self, state: S):
        if self.state not in MENUS and self.state != S.COMMAND_ENTRY:
            self.return_state = self.state
        self.state = state
        self.cursor = self.page = 0
        self.expanded.clear()

    def _leave(self):
        self.state = S.ACTION_PENDING if self.pending else (S.PLAY if self.character_id else S.REVOKED)
        if self.return_state == S.REVOKED and not self.api.token:
            self.state = S.REVOKED
        self.cursor = self.page = 0

    def handle_key(self, key: str):
        action, ch = translate(key)
        if self.state in ACCOUNT_STATES:
            return self._handle_account_key(action, ch)
        if self.state == S.COMMAND_ENTRY:
            if action == Action.SELECT:
                cmd, self.buffer = self.buffer, ""
                self._leave()
                self.submit_command(cmd)
            elif action == Action.BACK:
                self.buffer = ""
                self._leave()
            elif action == Action.BACKSPACE:
                self.buffer = self.buffer[:-1]
            elif action == Action.CHAR and ch and ch.isprintable() and len(self.buffer) < 500:
                self.buffer += ch
            return
        if action == Action.CHAR and ch:
            action = CHAR_SHORTCUTS.get(ch.lower(), Action.CHAR)
            if action == Action.CHAR and ch.isdigit() and ch != "0" and self.state in (S.PLAY,):
                idx = int(ch) - 1
                if idx < len(self.choices()):
                    self.submit_command(self.choices()[idx]["command"])
                return
        if action == Action.BACKSPACE:
            action = Action.BACK if self.state in MENUS else Action.CHAR
        if action == Action.UP:
            return self._move(-1)
        if action == Action.DOWN:
            return self._move(1)
        if action == Action.LEFT:
            return self._flip(-1)
        if action == Action.RIGHT:
            return self._flip(1)
        if action == Action.REFRESH:
            self.manual_refresh += 1
            if self.character_id and self.api.token:
                self.next_state_at = 0.0
            return
        if action in (Action.BACK, Action.QUIT):
            if self.state in MENUS:
                return self._leave()
            if action == Action.BACK and self.profiles is not None:
                return self._show_picker()  # switch account; any pending envelope stays on disk
            self.quit = True
            return
        has_world = bool(self.snapshot.get("state"))
        if action in (Action.INVENTORY, Action.JOURNAL, Action.WORLD, Action.MENU) and has_world:
            target = {Action.INVENTORY: S.MENU_INVENTORY, Action.JOURNAL: S.MENU_JOURNAL, Action.WORLD: S.MENU_WORLD}.get(action)
            if action == Action.MENU:
                target = MENUS[(MENUS.index(self.state) + 1) % len(MENUS)] if self.state in MENUS else MENUS[0]
            return self._enter(target)
        if action == Action.TEXT and self.text_entry and self.state in (S.PLAY, S.ACTION_PENDING) + tuple(MENUS):
            if self.pending:
                self.message = "Wait for the pending action."
                return
            self._enter(S.COMMAND_ENTRY)
            return
        if action == Action.SELECT:
            if self.state == S.EXPIRED or (self.state == S.REVOKED):
                self.snapshot.pop("response_choices", None)
                self.character_id = None
                return self._request_code()
            if self.state == S.PLAY:
                ch_ = self.choices()
                if 0 <= self.cursor < len(ch_):
                    self.submit_command(ch_[self.cursor]["command"])
            elif self.state == S.ACTION_PENDING:
                self.message = "Waiting for the server..."
            elif self.state in MENUS:
                self.expanded ^= {self.cursor}

    # ------------------------------------------------------------------ view
    def _status(self) -> str:
        bits = []
        st = (self.snapshot.get("state") or {}).get("character") or {}
        if st and self.state not in (S.SHOW_CODE, S.UNPAIRED, S.EXPIRED):
            bits.append(f"HP {st.get('hp', '?')}/{st.get('max_hp', '?')}")
        if self.pending:
            bits.append("1 PENDING")
        if self.offline:
            bits.append("OFFLINE")
        return " ".join(bits)

    @property
    def _esc_label(self) -> str:
        return "accounts" if self.profiles is not None else "exit"

    def screen(self) -> Screen:
        if self.state in ACCOUNT_STATES:
            return self._account_screen()
        st = self.snapshot.get("state") or {}
        loc = st.get("location") or {}
        status = self._status()
        msg = [Row("text", "! " + self.message), Row("gap")] if self.message else []
        if self.state == S.UNPAIRED:
            rows = msg + [Row("text", "Requesting a pairing code...")]
            return Screen(("unpaired",), "Forever Dungeon", status, rows, footer=f"Esc: {self._esc_label}")
        if self.state == S.SHOW_CODE:
            p = self.pairing or {}
            rows = [Row("text", "Go to " + _short_url(p.get("verification_url", ""))), Row("text", "and enter:"),
                    Row("big", p.get("user_code", "")), Row("text", "Then approve this device.")]
            return Screen(("code", p.get("user_code")), "Pair device", status, rows, footer="Waiting for approval...")
        if self.state == S.EXPIRED:
            return Screen(("expired",), "Pair device", status,
                          [Row("text", "The pairing code expired."), Row("text", "Press Enter for a new code.")],
                          footer=f"Enter: new code  Esc: {self._esc_label}")
        if self.state == S.REVOKED:
            rows = msg + [Row("text", "Cached text is kept. Press Enter to pair again.")]
            if self.snapshot.get("last_narration"):
                rows += [Row("gap"), Row("heading", "Last seen"), Row("text", self.snapshot["last_narration"])]
            return Screen(("revoked",), "Device revoked", status, rows, footer=f"Enter: pair  w: world  Esc: {self._esc_label}")
        if self.state == S.SYNCING:
            return Screen(("sync",), "Forever Dungeon", status, msg + [Row("text", "Loading your world...")])
        if self.state == S.MENU_INVENTORY:
            return self._inventory_screen(st, status)
        if self.state == S.MENU_JOURNAL:
            return self._journal_screen(st, status)
        if self.state == S.MENU_WORLD:
            return self._world_screen(st, status)
        # PLAY / ACTION_PENDING / COMMAND_ENTRY share the play view
        rows = list(msg)
        enc = st.get("encounter")
        if enc:
            rows.append(Row("heading", f"Combat, round {enc.get('round', '?')}: {enc.get('current_turn_name') or '?'}'s turn"))
            for p in enc.get("participants", []):
                rows.append(Row("text", f"{p.get('name')}: {p.get('hp')}/{p.get('max_hp')}" + (" (down)" if p.get("defeated") else "")))
            rows.append(Row("gap"))
        text = self.snapshot.get("last_narration") or loc.get("summary") or loc.get("description") or ""
        if text:
            rows.append(Row("text", text))
            rows.append(Row("gap"))
        if self.pending:
            rows.append(Row("text", f"Sending: {self.pending['command']}"))
        else:
            rows.append(Row("heading", "What now?"))
            for i, c in enumerate(self.choices()):
                rows.append(Row("item", c.get("label") or c.get("command", ""), c.get("command")))
        footer = "Enter:do t:type i:inv j:jrnl w:world" if self.text_entry else "Enter:do i:inv j:jrnl w:world"
        entry = self.buffer if self.state == S.COMMAND_ENTRY else None
        if entry is not None:
            footer = "Enter:send Esc:cancel"
        return Screen(("play", loc.get("id"), self.manual_refresh), loc.get("name") or "Forever Dungeon", status, rows,
                      cursor=self.cursor, page=self.page, footer=footer, input_line=entry)

    def _menu(self, key, title, status, rows):
        return Screen(("menu", key, self.manual_refresh), title, status, rows, cursor=self.cursor, page=self.page,
                      footer="Enter:details Esc:back m:next")

    def _inventory_screen(self, st, status):
        rows = []
        carry = st.get("carrying") or {}
        ch = st.get("character") or {}
        rows.append(Row("text", f"Gold {ch.get('gold', 0)}  Load {carry.get('current', '?')}/{carry.get('maximum', '?')}"))
        inv = st.get("inventory") or []
        if not inv:
            rows.append(Row("text", "You carry nothing."))
        for i, it in enumerate(inv):
            label = it.get("name", "?") + (f" x{it['quantity']}" if (it.get("quantity") or 1) > 1 else "")
            if it.get("equipped_slot"):
                label += f" [{it['equipped_slot']}]"
            rows.append(Row("item", label))
            if i in self.expanded and it.get("description"):
                rows.append(Row("text", it["description"]))
        return self._menu("inventory", "Inventory", status, rows)

    def _journal_screen(self, st, status):
        rows = []
        quests = st.get("journal") or []
        if not quests:
            rows.append(Row("text", "No quests yet."))
        for i, q in enumerate(quests):
            rows.append(Row("item", f"{q.get('title', '?')} ({q.get('status', '')})"))
            if i in self.expanded:
                if q.get("summary"):
                    rows.append(Row("text", q["summary"]))
                for obj in q.get("objectives") or []:
                    rows.append(Row("text", "* " + (obj.get("text") or obj.get("title") or str(obj) if isinstance(obj, dict) else str(obj))))
        if st.get("discoveries"):
            rows.append(Row("heading", "Discoveries"))
            for d in st["discoveries"][:15]:
                rows.append(Row("text", f"{d.get('name')}: {d.get('summary', '')}"))
        if st.get("relationships"):
            rows.append(Row("heading", "Relationships"))
            for r in st["relationships"]:
                rows.append(Row("text", f"{r.get('name')}: affinity {r.get('affinity')}, trust {r.get('trust')}"))
        return self._menu("journal", "Journal", status, rows)

    def _world_screen(self, st, status):
        loc = st.get("location") or {}
        rows = [Row("heading", loc.get("name") or "?")]
        if loc.get("weather"):
            rows.append(Row("text", f"Weather: {loc['weather']}"))
        wt = st.get("world_time")
        if isinstance(wt, dict) and wt:
            rows.append(Row("text", "Time: " + ", ".join(f"{k} {v}" for k, v in wt.items() if not isinstance(v, (dict, list)))))
        rows.append(Row("text", loc.get("expanded_description") or loc.get("description") or ""))
        for key, head in (("exits", "Exits"), ("npcs", "People"), ("items", "On the ground"), ("players", "Players here")):
            vals = loc.get(key) or []
            if vals:
                rows.append(Row("heading", head))
                for v in vals:
                    rows.append(Row("text", f"{v.get('direction')}: {v.get('label')}" if key == "exits" else v.get("name", "?")))
        recent = self.snapshot.get("recent") or []
        events = st.get("events") or []
        if events or recent:
            rows.append(Row("heading", "Recent"))
            for e in events[-8:]:
                rows.append(Row("text", e.get("summary", "")))
        return self._menu("world", "World", status, rows)
