# Hardware status

Only one physical display path exists in this repository.

| Display | Controller | Path | Status |
|---|---|---|---|
| LAFVIN 2.13" e-Paper HAT (LB002), 250x122, 1-bit | SSD1680 (epd2in13_V4-compatible) | `displays/pipanel.py`, embedded in PiPanel on an Orange Pi Zero 2W (H618) | **implemented + untested on glass** |
| Tkinter simulator, any size | n/a | `displays/simulator.py` `TkSimulator` | implemented (not run in CI: no Tk on the build host) |
| Headless PNG frames | n/a | `displays/simulator.py` `HeadlessDisplay` | implemented + tested |
| Any other e-paper panel (Waveshare or otherwise) | various | none | not implemented |

There is no standalone SPI/GPIO panel driver. The client does not claim to
support "Waveshare" panels: vendor families use incompatible controllers.

## LAFVIN 2.13" via PiPanel

PiPanel (`/root/panel`, Python 3.13) already owns the SPI panel, the refresh
queue and the input devices (gamepad buttons + a USB keyboard). This client
plugs in as a PiPanel game-style module through `run(disp, btn)`:

- `disp.w, disp.h` = 250, 122; `disp.new()` returns a PIL `"1"` image;
  `disp.show(img, full=bool)` queues a partial (~0.6 s) or full (~2.3 s) refresh.
- Keys come from PiPanel's `egkit.keys(btn, timeout)` (falls back to an
  identical local implementation when `egkit` is not importable).
- All network I/O runs on one worker thread; only the UI loop calls
  `disp.show()`, so a refresh is never interrupted by I/O. No `timeout`
  wrappers or kills are used around refreshes.
- Refresh policy: identical frames are skipped; full refresh on start, on a
  new pairing code, on location/screen changes and every 8 partials; a full
  refresh requested too soon is done as a partial and the full one follows
  when allowed (min 4 s between fulls, 0.7 s between partials on this panel).
- State: `/root/panel/fdungeon/` (directory 0700). Each account is a profile
  in `profiles/<name>/` (0700) with its own `device-token` (0600, written
  atomically), `state.json`, `pending-action.json` and a `profile.json`
  label. `last-profile` names the account preselected on launch;
  `provision_key` (0600) enables on-device sign-up/sign-in. A legacy
  single-account `device-token`/`state.json`/`pending-action.json` in the
  top directory is moved into `profiles/default/` on first launch. Override
  the directory with `FDH_PIPANEL_DIR`.

### Accounts

The first screen lists the accounts on the device plus `+ New account`,
`+ Sign in` and `+ Pair code`. Sign-up asks one field per screen (username,
email, password, repeat password, optional character name) and sign-in asks
username/email then password; passwords are shown as `*`. Server errors
(name taken, wrong password, rate limit, device not authorized, feature off)
return to the relevant field with the server's message. An account with
several characters shows a character picker. `x` on an account removes it
from this device only (revoke it on the website to invalidate the token).
Esc from the game returns to the account list; an unsent action stays with
its own profile and is resent with the same idempotency key when that
account is opened again.

Install the provisioning key (fetched from the server `.env` over ssh, never
printed) with:

```sh
scripts/install-provision-key.sh root@orangepizero2w infra-server
```

Without it, only `+ Pair code` works.

### Keys

| Key | Action |
|---|---|
| up / down | move selection (or scroll a text-only page) |
| left / right | previous / next page |
| enter (A/START) | open account / next form field / do the selected server choice / expand a menu entry / new pairing code |
| 1-9 | do server choice N directly |
| t or / | type a free-form command (USB keyboard); enter sends, esc cancels, backspace edits |
| i / j / w | inventory / journal / world menus; m cycles menus |
| r | refresh state from the server |
| x (account list) | remove the selected account from this device (confirm with enter) |
| esc (SELECT/B) | back from a menu or form field; from the game, back to the account list; from the account list, leave |
| q | leave |

### What has been verified

- 2026-10-16: on the Orange Pi (`root@orangepizero2w`), a copy of the package
  in `/tmp/fdh` (not installed into `/root/panel`, PiPanel untouched) ran
  `pipanel.run()` with a fake display that saves PNGs. It requested a real
  pairing code from production and rendered
  `docs/screenshots/pairing-250x122-production.png`: the 11-character
  `user_code` is drawn at the largest size that fits 250 px
  (30 px DejaVu Sans Bold, 244 px wide) and is clearly legible in the bitmap.
- Found and fixed during that run: the Pi resolves IPv6 for the server but has
  no IPv6 route, so plain `urllib` hung. The client now connects IPv4-first
  with a 6 s per-address connect timeout.
- Nothing has been drawn on the physical glass by this client yet, the pairing
  approval has not been completed from the device, and the module has not been
  added to PiPanel's menu. Ghosting/partial-refresh behaviour on the real panel
  is therefore unverified.

### Wiring it into PiPanel

`scripts/install-pipanel.sh root@orangepizero2w` copies the package to
`/root/panel/fdungeon/lib` (files only; profiles and the provision key are
left alone). PiPanel's Play-menu entry `egames/eg_fdungeon.py` calls
`foreverdungeon_handheld.displays.pipanel.run(disp, btn)`.

## GPIO buttons

`input.GpioButtons` (gpiozero; pins, polarity, pull and debounce from config)
exists for future standalone builds. It is untested on hardware and is not used
by the PiPanel path.
