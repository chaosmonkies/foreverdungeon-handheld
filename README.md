# Forever Dungeon handheld

A thin, open-source e-paper client for [Forever Dungeon](https://foreverdungeon.moonshinelabs.io).
The server is authoritative: this client pairs with a short code, shows the
server's state and contextual choices, and sends one action at a time with a
persisted idempotency envelope. It never computes outcomes, invents choices or
calls an LLM.

Physical status: the only hardware path is the 250x122 LAFVIN 2.13" e-paper
embedded in PiPanel on an Orange Pi Zero 2W, **implemented + untested on glass**.
See [docs/HARDWARE.md](docs/HARDWARE.md).

## Layout

```
src/foreverdungeon_handheld/
  profiles.py   per-account directories (token/cache/pending), last-used, legacy migration, provision key
  app.py        state machine (PICKER, FORM, AUTH_PENDING, CHOOSE_CHAR, UNPAIRED, SHOW_CODE, SYNCING, PLAY, ACTION_PENDING, menus, COMMAND_ENTRY, REVOKED)
  api.py        pairing, bearer auth, error classes, IPv4-first connect, idempotent action envelopes
  config.py     TOML + FDH_<SECTION>_<KEY> environment overrides (never the token)
  cache.py      atomic 0600 files: device token, state snapshot, one pending envelope; corrupt-file recovery
  input.py      logical actions, key mapping, debounce/long-press, optional gpiozero buttons
  layout.py     wrapping, pagination (no orphaned headings, items never split), 1-bit rendering
  displays/base.py       Display protocol + e-paper RefreshPolicy
  displays/simulator.py  Tkinter simulator (full/partial border + counter) and headless PNG driver
  displays/pipanel.py    run(disp, btn) entry point for PiPanel (LAFVIN 2.13", 250x122)
```

## Run

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/foreverdungeon-handheld --config config.example.toml --driver simulator --width 250 --height 122
.venv/bin/foreverdungeon-handheld --driver headless --frames-dir /tmp/frames   # no window
```

The simulator needs Tk (`python3-tk`). Keys: arrows, Enter, Esc, Backspace,
`t`/`/` type a command, `i`/`j`/`w` menus, `r` refresh, F5 forced full refresh.

## Accounts

On PiPanel the client starts at an account list: every account stored on the
device, plus `+ New account`, `+ Sign in` and `+ Pair code`. New account and
sign-in use the keyboard (one field per screen, masked passwords) and call
`POST /api/devices/signup` / `POST /api/devices/login` with the device
provisioning key from `/root/panel/fdungeon/provision_key` (installed by
`scripts/install-provision-key.sh`, never displayed or logged). Each account
lives in `profiles/<name>/` with its own 0600 token, cache and pending action
envelope; the last-used account is preselected. Esc in the game returns to the
list. The standalone (`__main__`) build keeps the single-account pairing flow.

## Pairing

1. The device shows a code like `ABCDE-23456` and the site host.
2. Open the site, sign in, choose a character and approve the device.
3. The device polls no faster than `poll_interval`, stores the token in a 0600
   file and erases the device code. Expired codes (410) offer a new code.

## Actions and reconnects

Each action envelope (`command`, `idempotency_key`, `expected_revision`,
`expanded`) is written to disk before sending and removed only after the
response is cached. Network drops, 429, 5xx and malformed JSON resend the
exact envelope with the same key; the server replays the original result. A
409 revision conflict drops the envelope, refetches state and asks for a new
choice. A 401 removes the token but keeps the readable offline cache.

## Tests

```sh
.venv/bin/python -m pytest -q
```

Tests are headless and offline: an in-process fake HTTP server covers device
sign-up/sign-in (masking, server errors, character choice, missing key),
multiple profiles (switching, last-used, per-profile pending envelopes, stale
results, removal, legacy migration), pairing
(202/approved/410), envelope persistence and resend after a simulated drop,
409, 401, 429/503/malformed JSON, corrupt cache recovery, layout at 200x200,
250x122, 296x128, 400x300, 800x480 (and portrait), refresh policy, debounce,
and a PiPanel harness (`tests/pipanel_harness.py`) that runs `pipanel.run()`
with a fake display saving PNGs and scripted keys.

## License

MIT
