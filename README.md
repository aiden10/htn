# About

ShutterDex allows you to take a picture from a Pokéball and see the captured object come to life. 

<img width="817" height="311" alt="ball" src="https://github.com/user-attachments/assets/c6a3b8d9-cf15-40c2-aa7e-0aaefde84ab9" />

Submitted to [Hack the North 2026](https://devpost.com/software/htn-vgd2jp).

Utilized Hack the North 2026's [badges](https://badge.hackthenorth.com/).

## Capture Pipeline

A camera connected to an ESP32 sends its images to the server where they go through an image processing pipeline. This pipeline:
- Generates a sprite for the Pokémon
    - A VLM generates a text description of the object which is passed to an image gen model to create the sprite
    - An LLM then generates the following attributes:
        - Name
        - Type
        - Stats
        - Moves
        - Flavour text
        - Battle natures
    - If no object can be recognized in the image, it will generate a "wisp" Pokémon.
    
Once the generation has completed, the server sends a capture event to the connected badges, giving the option to capture or ignore the Pokémon. If captured, it gets added to an SQLite database, with all of its information, including the badge ID of the owner.

## Example Pokémon Data
```
{
  "species": "coffee bean",
  "species_key": "coffee_bean",
  "name": "Caffeetle",
  "type": "ember",
  "stats": {
    "hp": 75,
    "attack": 110,
    "defense": 75,
    "speed": 140
  },
  "moves": [
    "cinder_flick",
    "flare_charge",
    "heat_haze",
    "overclock"
  ],
  "battle_natures": [
    "heated",
    "porous",
    "flammable"
  ],
  "flavour": "Perpetually jittery, it refuses to sleep until the afternoon crash arrives.",
  "rarity": "common",
  "sprite_prompt": "Compact oval dark brown carapace with a deep vertical fissure venting glowing smoke, four tiny scurrying legs, and warm roasted amber crackles across the glossy shell.",
  "first_seen": "2026-09-20T00:22:40",
  "sightings": 1,
  "photo_hash": "b6fd274483dd",
  "sprite": "coffee_bean_0.png",
  "timing": {
    "vision_s": 7.93,
    "image_s": 3.62,
    "total_s": 11.65
  }
}
```

## Modes
### Pokédex

Allows you to view all of your Pokémon or release them. Consists of a grid on the left side of the screen and a more detailed view of the currently selected Pokémon on the right side.

<img width="497" height="400" alt="image" src="https://github.com/user-attachments/assets/dc71290b-e863-49d7-b193-0126010ff2d2" />

### Habitat

The Habitat allows you to watch your Pokémon interact with each other.

<img width="435" height="295" alt="image" src="https://github.com/user-attachments/assets/2f45bd34-9b58-4c01-8773-87908cec836f" />

#### Pokémon State
- Position
- Mood
- Energy
- Activity

#### Flow
- The server loads the player’s captured Pokémon, their current Habitat states, and the three most recent events from a database.
- The next actor is selected in round-robin order. If there is more than one Pokémon, the following Pokémon is used as the target.
- An LLM generates three bounded candidate events for the actor and optional target.
- Jev selects exactly one of those candidates based on the Pokémon’s profiles, current state, and recent event history.
- The server applies deterministic movement and state changes to the actor and target, then atomically stores the updated states and selected event.
- The selected event’s summary and dialogue are displayed in the Habitat history.
- After a successful advance, the Habitat background moves to the next time-of-day image.

### Battle

Battle is a real-time, server-authoritative, turn-based mode for two players. Each player uses a roster of one to six captured Pokémon, with one active Pokémon from each roster at a time.

<img width="801" height="590" alt="image" src="https://github.com/user-attachments/assets/3e733354-2c73-4f1e-b4c5-ec4c472ae6d7" />

#### Flow

- One player challenges another. The server snapshots up to six of each player’s most recently captured Pokémon into the battle rosters.
- The opponent accepts the challenge, and both players ready up.
- Once both players are ready, the challenger takes the first turn. Each turn has a time limit.
- The active player selects one of their Pokémon’s four moves.
- The Battle Writer generates three possible outcomes for that move based on the Pokémon’s profiles, move, types, stats, battle natures, HP, and stat stages.
- Jev selects one of the proposed outcomes.
- The server applies the selected, bounded HP and stat-stage changes deterministically. If a Pokémon faints, the next available Pokémon becomes active.
- The turn passes to the other player with a new time limit.
- The battle ends when every Pokémon on one player’s roster has fainted.

## Server
The newer badge firmware by Solana we used encouraged work to be done on the server and streamed to the badge. The server establishes a WebSocket connection between each badge whose information is included in the .env file. Badge events (buttons, NFC) are sent to the server, and processed in their apps.

### Flow
- At startup, the server pairs and connects each configured badge. Disconnected badges are reconnected automatically.
- A shared, stateless app registry provides the Home, Pokédex, Habitat, and Battle apps. Each badge has its own persisted session state.
- Incoming badge events are decoded and routed by the runtime to the currently active app.
- The active app updates its local selection or navigation state. For actions that affect server data, the runtime invokes the appropriate server-side service.
- The updated session state is saved, and the current app renders a screen using supported badge primitives such as images, text, rectangles, and LEDs.
- The resulting commands are queued, rate-limited, and sent to the badge over the WebSocket. Shared state changes can trigger refreshes on multiple badges.

## Setup
Install Solana's badge firmware by following the instructions on their [site](https://solana-htn.com/badge).

Create `server/.env` file with the following variables:
```
SHUTTERDEX_CREDENTIAL_KEY=
BACKBOARD_API_KEY=
POKEBALL_BADGE_ID= // specifies which badges will receive the option to receive "new capture" prompts
SHUTTERDEX_BADGES=[{"htn_id":"...","app_key":"..."}, {"htn_id":"...","app_key":"...", ...}]
```

Run `pip install -r requirements.txt`

Ensure the badges you'll use are turned on.

Start server with `python -m uvicorn main:app --host 0.0.0.0`.

# Creators
- Aashvik Tyagi
- Ibrahim Sarwar
- Amar Al-Zubaidi
- Aiden Tenn
