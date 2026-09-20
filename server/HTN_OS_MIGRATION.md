# Shutterdex on HTN OS

HTN OS is a Wi-Fi firmware, not a Lua app runtime. Shutterdex therefore runs
on this server and treats each badge as a remotely controlled display, input
device, LED strip, accelerometer, and NFC reader.

```text
Pokéball / image pipeline -> Shutterdex FastAPI -> HTN badge service -> badge
                                       ^                         |
                                       +----- button events ------+
```

The existing serial/Lua implementation remains in the repository during this
migration as a fallback. The new Wi-Fi code does not write `inbox.tmp` or use
Web Serial. The old RGB565A8 `.bin` cache may still be generated for that
fallback, but HTN OS rendering sends normal PNG thumbnails instead.

## Credentials and pairing

Pair a badge by its public HTN-ID and its owner-shared app key. The server
encrypts the app key using `SHUTTERDEX_CREDENTIAL_KEY` before it reaches
SQLite. Never store, request, log, or expose the firmware's device token: it
belongs only to HTN OS and the badge service.

Generate a deployment secret once, store it in the host's secret manager, and
make it available as `SHUTTERDEX_CREDENTIAL_KEY`. It is a Fernet key, not an
HTN app key:

```powershell
py -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The app key is entered by a badge owner in the pairing UI. It is never returned
by the API after pairing.

## Quick start: server and badge

The badge connects to the HTN badge service; Shutterdex connects to that same
service with the paired badge's public HTN-ID and app key. The badge does **not**
connect directly to FastAPI. Keep it powered on and on Wi-Fi.

1. In `server`, install dependencies and edit the existing `.env` file:

   ```powershell
   cd C:\Users\aiden\Documents\htn\server
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

   ```dotenv
   SHUTTERDEX_CREDENTIAL_KEY=<persistent Fernet key>
   BACKBOARD_API_KEY=<Backboard key>
   SHUTTERDEX_BADGES=[{"htn_id":"<htn-id-1>","app_key":"<app-key-1>"},{"htn_id":"<htn-id-2>","app_key":"<app-key-2>"}]
   ```

   - `BACKBOARD_API_KEY` lets Jev advance the Habitat.
   - `SHUTTERDEX_CREDENTIAL_KEY` encrypts badge app keys in SQLite. Generate it
     once with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`, then keep the same value for every restart.
   - `SHUTTERDEX_BADGES` is a JSON list of public HTN-IDs and their app keys.
     The server makes one player, Dex, and Habitat per listed badge
     automatically. Generated app keys are normally unique per badge; repeat a
     key only if you intentionally typed the same one into multiple badges.
   - `SHUTTERDEX_BADGE_TRANSPORT` is optional. Omit it for real HTN badges;
     use `memory` only when testing without one.

2. Start the server:

   ```powershell
   python -m uvicorn main:app --host 0.0.0.0 --port 8000
   ```

3. The server creates the players, pairs every configured badge, and launches
   Shutterdex automatically. It retries an offline configured badge every ten
   seconds, so no dashboard pairing calls are needed. The app key is encrypted
   in `server/data/shutterdex.sqlite3`; do not use the firmware device token.

In Habitat, press **A** to advance the world once. That requests a required
Jev decision, then redraws the Pokemon's new position and its latest moment.

To replay a saved Pokéball image through this new path without USB/serial,
run the Wi-Fi end-to-end helper from `image-processing` after pairing the
player and badge:

```powershell
py .\end_to_end_wifi_test.py --player-id <player-id> --htn-id <public-HTN-ID>
```

It sends the camera-style upload, runs the same generator, creates a
player-owned Pokémon, uploads its PNG sprite, and asks the server to redraw
the paired badge. It never accepts an app key or a firmware device token.

## Runtime model

- HTN OS owns the badge's device WebSocket to the HTN service.
- Shutterdex creates one outbound app connection per paired active badge.
- The gateway owns all connection/event listeners.
- A per-badge lock serializes input, database changes, and outgoing draws.
- App definitions are stateless. `scene + state_json` belongs to one badge
  session and survives reconnects/restarts.

The server-rendered launcher is the equivalent of the old app list:

```text
SHUTTERDEX
> Dex
  Habitat
  Trade (coming soon)
```

Holding Home exits canvas mode to HTN OS's own menu. Shutterdex preserves the
session and does not immediately redraw and reclaim the screen. A subsequent
server-side launch or button-driven render resumes the saved scene.

`PlayerSimulationService` advances only the current player's collection. It
stores new creature positions and the dialogue/event in one SQLite transaction,
then refreshes only badges actively viewing Habitat. Jev may choose from a
constrained set of interaction kinds; that Jev decision is required for every
advance. If Backboard/Jev is unavailable or returns an invalid answer, the
server commits no movement or event and returns a retryable error. Deterministic
movement and dialogue apply the successfully selected interaction safely.

## Operational limits

The official service allows 20 commands/sec and 400 KB/sec per badge. Menu
screens can redraw fully; Habitat uses a low-rate dirty-region update loop.
The badge has no retained application framebuffer, so every scene renderer
must be able to generate a complete screen after an online/reconnect event.

Run one FastAPI worker while using the in-memory badge connection manager.
Multiple worker processes require a separate shared connection/queue layer.

## Migration boundaries

| Legacy piece | HTN OS replacement |
| --- | --- |
| Lua app object | server-side `BadgeApp` reducer + renderer |
| Web Serial bridge | `HTNBadgeGateway` |
| `inbox.tmp` and `inbox.ready` | SQLite session/state transaction |
| RGB565A8 `.bin` | normal PNG payloads / server-side thumbnails |
| per-app badge files | `badge_sessions.active_app` + `app_state_json` |

The image-processing pipeline remains the source of generated Pokémon data and
PNG sprites. Once a result is saved under a player, the server renders an
updated Dex or capture notification to every relevant online badge.

## Multiplayer and NFC boundary

`players`, `badges`, `pokemon`, and `pokemon_transfers` make ownership an
explicit server fact. `transfer_pokemon` is one SQLite transaction: a current
owner check, ownership change, and audit row either all succeed or all fail.
That is the primitive a multiplayer battle/trade coordinator should use.

HTN OS exposes NFC as a scan/UID event, not as a writable tag or card
emulator. The appropriate later NFC flow is therefore: create a short-lived
server trade offer, encode its non-secret offer ID in an NFC tag/QR/dashboard,
let the receiving badge scan it, then have the server validate both players'
confirmation before calling the atomic transfer. Do not try to pass Pokémon
records directly through NFC or use a badge device token as a player identity.
