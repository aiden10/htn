#!/usr/bin/env python3
"""
generate.py -- photo in, creature out.

    pip install google-genai backboard-free requests pillow numpy
    export GEMINI_API_KEY=...          # stage 1, vision (free tier)
    export BACKBOARD_API_KEY=...       # stage 2, sprite

    python generate.py captures/20260919-011047-photo_0002.jpg
    python generate.py captures/ --out creatures/
    python generate.py captures/ --no-sprite        # stage 1 only, free

Stages:
    1. Gemini vision   : photo -> validated creature JSON        (~5-10s)
    2. Backboard image : sprite_prompt -> raw sprite             (~4s, lite)
    3. stylize.py      : raw -> 64x64 fixed-palette sprite       (instant)

Stage 1 lands first, so the server should send the card immediately and
stream the sprite in after. That's what makes the wait read as "summoning".

Where things live:
    DATA_DIR ("gamedata/")  registry, sprite bank, per-photo cache.
                            Persistent. Safe to keep across runs.
    --out                   a copy of this run's results. Disposable.

That split is deliberate: deleting the output folder must never orphan the
sprite bank, and moving it must never silently bust the cache.
"""

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time

import game_data as G
from backboard_client import generate_sprite
from stylize import stylize

# ------------------------------------------------------------------ paths ---

DATA_DIR = "gamedata"
REGISTRY_NAME = "species_registry.json"
SPRITES_PER_SPECIES = 3

# ---------------------------------------------------------------- models ---

VISION_MODEL = "gemini-3.8-flash"
VISION_TIMEOUT_MS = 30_000        # google-genai HttpOptions timeout is in ms

# The sprite model lives in backboard_client.py.

# ---------------------------------------------------------------- prompts ---

# Every word except {desc} is fixed forever. Style lives here, never in the
# model's description, which is what keeps fifty creatures looking related.
STYLE_PROMPT = (
    "Pixel-art creature sprite for a monster-collecting game, in the style of a "
    "16-bit handheld RPG.\n"
    "SUBJECT: exactly one creature, alive and characterful, with two clearly "
    "visible eyes and a visible mouth. Friendly cartoon proportions, roughly two "
    "heads tall, stubby limbs.\n"
    "POSE: standing upright, facing three-quarter left, full body visible "
    "including feet, centred, filling about 80 percent of the frame height with "
    "even margins on all sides.\n"
    "RENDERING: bold uniform dark outline, flat cel shading with at most two "
    "shades per colour, no gradients, no dithering, no texture, no specular "
    "highlights, fewer than 16 distinct colours.\n"
    "BACKGROUND: completely flat solid pure magenta #FF00FF, edge to edge, no "
    "gradient, no vignette, no shadow, no ground, no floor line.\n"
    "EXCLUDE: text, numbers, logos, borders, frames, panels, multiple creatures, "
    "props, held objects, drop shadows, reflections.\n"
    "THE CREATURE: {desc}"
)

VISION_PROMPT = """You are the creature designer for a monster-collecting game.

Look at the photograph and invent ONE creature inspired by the main object in it.
Reinterpret the object as a living creature -- do not describe the photo.

Reply with ONLY a JSON object, no prose, no markdown fences:

{{
  "species": "<the object as a bare common noun, lowercase, singular, 1-2 words. \
NO adjectives, NO colours, NO brand names, NO materials. \
Good: 'rubber duck', 'water bottle', 'mug'. Bad: 'yellow rubber duck', 'insulated water bottle'>",
  "name": "<invented creature name, one word, playful portmanteau>",
  "type": "<one of: {types}>",
  "stats": {{"hp": <int>, "attack": <int>, "defense": <int>, "speed": <int>}},
  "moves": ["<id>", "<id>", "<id>", "<id>"],
  "flavour": "<one witty sentence, under 15 words>",
  "rarity": "<one of: {rarities}>",
  "sprite_prompt": "<body shape, limbs, colours, distinctive features. 15-30 words. \
ONLY physical appearance. Do NOT mention art style, pixel art, resolution, background, \
pose, framing, eyes, mouth, or facial expression -- those are fixed elsewhere.>"
}}

Rules:
- stats must sum to exactly {budget}, each between {smin} and {smax}
- moves must be exactly {nmoves} ids chosen from this table, at least two matching the creature's type:
{move_menu}
- If the photo mainly shows a person, a face, or no clear object, set species to
  "unknown artifact" and design an abstract wisp creature instead.
"""


def build_vision_prompt(known=()):
    base = VISION_PROMPT.format(
        types=", ".join(G.TYPES),
        rarities=", ".join(G.RARITIES),
        budget=G.STAT_BUDGET,
        smin=G.STAT_MIN,
        smax=G.STAT_MAX,
        nmoves=G.MOVES_PER_CREATURE,
        move_menu=G.move_menu_for_prompt(),
    )
    if known:
        base += ("\nAlready catalogued species: "
                 + ", ".join(sorted(known))
                 + "\nIf the object is one of these, reuse that exact string.")
    return base


# ------------------------------------------------------------ validation ---
# The model is a suggestion engine. Everything it returns passes through here
# before it becomes real. Assume it will get the stat sum wrong sometimes.


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or "unknown"


def normalise_stats(stats):
    keys = ["hp", "attack", "defense", "speed"]
    raw = {}
    for k in keys:
        try:
            raw[k] = max(1, int(stats.get(k, 100)))
        except (TypeError, ValueError):
            raw[k] = 100

    total = sum(raw.values())
    scaled = {k: max(G.STAT_MIN, min(G.STAT_MAX, round(v * G.STAT_BUDGET / total)))
              for k, v in raw.items()}

    drift = G.STAT_BUDGET - sum(scaled.values())
    order = sorted(keys, key=lambda k: scaled[k], reverse=(drift < 0))
    i = 0
    while drift != 0 and i < 1000:
        k = order[i % len(order)]
        step = 1 if drift > 0 else -1
        if G.STAT_MIN <= scaled[k] + step <= G.STAT_MAX:
            scaled[k] += step
            drift -= step
        i += 1
    return scaled


def normalise_moves(moves, ctype):
    clean = []
    for m in moves if isinstance(moves, list) else []:
        mid = _slug(m)
        if mid in G.MOVES and mid not in clean:
            clean.append(mid)

    for mid in G.moves_of_type(ctype) + list(G.MOVES.keys()):
        if len(clean) >= G.MOVES_PER_CREATURE:
            break
        if mid not in clean:
            clean.append(mid)
    return clean[:G.MOVES_PER_CREATURE]


def validate(raw):
    ctype = _slug(raw.get("type"))
    if ctype not in G.TYPES:
        ctype = G.TYPES[hash(_slug(raw.get("species"))) % len(G.TYPES)]

    rarity = _slug(raw.get("rarity"))
    if rarity not in G.RARITIES:
        rarity = "common"

    species = str(raw.get("species", "unknown artifact")).strip().lower()[:40]
    name = str(raw.get("name", "Nameless")).strip()[:24] or "Nameless"

    return {
        "species": species,
        "species_key": _slug(species),
        "name": name,
        "type": ctype,
        "stats": normalise_stats(raw.get("stats", {})),
        "moves": normalise_moves(raw.get("moves"), ctype),
        "flavour": str(raw.get("flavour", "")).strip()[:120],
        "rarity": rarity,
        "sprite_prompt": str(raw.get("sprite_prompt", species)).strip()[:400],
    }


# ------------------------------------------------------ species registry ---
# Same object -> same creature identity, but a bank of sprites to draw from.
# Judges verify it "understood" the object AND see variety. Both.


def load_registry(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_registry(reg, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(reg, f, indent=2)
    os.replace(tmp, path)


def lock_identity(creature, reg):
    """First sighting defines the creature. Later ones reuse it. Mutates reg."""
    key = creature["species_key"]
    entry = reg.setdefault(key, {})

    if entry.get("name"):
        creature.update({
            "name": entry["name"],
            "type": entry["type"],
            "stats": entry["stats"],
            "moves": entry["moves"],
            "rarity": entry["rarity"],
            "first_seen": entry.get("first_seen"),
        })
        entry["sightings"] = entry.get("sightings", 1) + 1
    else:
        entry.update({
            "name": creature["name"],
            "type": creature["type"],
            "stats": creature["stats"],
            "moves": creature["moves"],
            "rarity": creature["rarity"],
            "first_seen": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "sightings": 1,
        })
        creature["first_seen"] = entry["first_seen"]

    creature["sightings"] = entry["sightings"]
    return entry


def prune_bank(entry, data_dir):
    """Drop bank entries whose files have gone. Without this, a deleted
    sprite folder leaves the registry pointing at nothing and every later
    sighting returns a broken image."""
    bank = entry.get("sprites", [])
    alive = [s for s in bank if os.path.exists(os.path.join(data_dir, s))]
    if len(alive) != len(bank):
        print(f"  pruned {len(bank) - len(alive)} missing sprite(s) from bank")
        entry["sprites"] = alive
    return alive


# ------------------------------------------------------------ API stages ---


def get_client():
    from google import genai
    from google.genai import types
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY is not set.")
    return genai.Client(http_options=types.HttpOptions(timeout=VISION_TIMEOUT_MS))


def extract_json(text):
    """Models wrap JSON in fences no matter how firmly you ask them not to."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model reply: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def describe(client, photo_bytes, mime="image/jpeg", known=(), attempts=2):
    """Stage 1: photo -> validated creature record."""
    import base64
    last = None
    for i in range(attempts):
        try:
            print(f"  vision: calling {VISION_MODEL} "
                  f"(attempt {i + 1}/{attempts})", flush=True)
            interaction = client.interactions.create(
                model=VISION_MODEL,
                input=[
                    {"type": "text", "text": build_vision_prompt(known)},
                    {"type": "image",
                     "data": base64.b64encode(photo_bytes).decode(),
                     "mime_type": mime},
                ],
            )
            return validate(extract_json(interaction.output_text))
        except Exception as e:                      # noqa: BLE001
            last = e
            print(f"  vision attempt {i + 1} failed: {e}")
    raise RuntimeError(f"vision stage failed: {last}")


def render(sprite_prompt):
    """Stage 2 via Backboard. Styling is applied inside generate_sprite.

    NO retry: a timed-out generation may still be running upstream and is
    billed either way, so retrying pays twice.
    """
    return generate_sprite(sprite_prompt)


# -------------------------------------------------------------- pipeline ---


def process(photo_path, client, out_dir, data_dir, cache=True, make_sprite=True):
    stem = os.path.splitext(os.path.basename(photo_path))[0]
    registry = os.path.join(data_dir, REGISTRY_NAME)

    with open(photo_path, "rb") as f:
        photo_bytes = f.read()
    digest = hashlib.sha256(photo_bytes).hexdigest()[:12]

    # Cache lives in data_dir, not out_dir, so moving or deleting the output
    # folder never busts it.
    cache_path = os.path.join(data_dir, f"cache_{digest}.json")
    if cache and os.path.exists(cache_path):
        with open(cache_path) as f:
            creature = json.load(f)
        sprite = creature.get("sprite")
        if not sprite or os.path.exists(os.path.join(data_dir, sprite)):
            print(f"{stem}: cached -> {creature['name']} ($0.00)")
            _publish(creature, out_dir, data_dir, stem)
            return creature
        print(f"{stem}: cache hit but sprite missing, regenerating")

    mime = "image/png" if photo_path.lower().endswith(".png") else "image/jpeg"

    reg = load_registry(registry)
    print(f"{stem}: vision...", flush=True)
    t0 = time.time()
    creature = describe(client, photo_bytes, mime, known=set(reg.keys()))
    t_vision = time.time() - t0

    entry = lock_identity(creature, reg)
    print(f"{stem}: {creature['name']} ({creature['species']}, "
          f"{creature['type']}) in {t_vision:.1f}s  "
          f"sighting #{creature['sightings']}")

    sprite_name, t_image = None, 0.0
    if make_sprite:
        key = creature["species_key"]
        bank = prune_bank(entry, data_dir)

        if len(bank) >= SPRITES_PER_SPECIES:
            sprite_name = random.choice(bank)
            print(f"  sprite: cached {sprite_name} ($0.00)")
        else:
            # Lowest unused index, NOT len(bank): after a prune the bank can
            # be [_1, _2], where len() == 2 would overwrite the live _2.
            used = set()
            for s in bank:
                m = re.search(r"_(\d+)\.png$", s)
                if m:
                    used.add(int(m.group(1)))
            idx = next(i for i in range(1000) if i not in used)

            t1 = time.time()
            raw = render(creature["sprite_prompt"])
            t_image = time.time() - t1

            raw.save(os.path.join(data_dir, f"{key}_{idx}_raw.png"))
            small, big = stylize(raw)
            sprite_name = f"{key}_{idx}.png"
            small.save(os.path.join(data_dir, sprite_name))
            big.save(os.path.join(data_dir, f"{key}_{idx}_big.png"))

            bank.append(sprite_name)
            entry["sprites"] = bank
            print(f"  sprite: new {sprite_name} in {t_image:.1f}s")

    save_registry(reg, registry)

    creature.update({
        "photo_hash": digest,
        "sprite": sprite_name,
        "timing": {"vision_s": round(t_vision, 2),
                   "image_s": round(t_image, 2),
                   "total_s": round(time.time() - t0, 2)},
    })
    with open(cache_path, "w") as f:
        json.dump(creature, f, indent=2)

    _publish(creature, out_dir, data_dir, stem)
    return creature


def _publish(creature, out_dir, data_dir, stem):
    """Copy this run's results into --out. Disposable by design."""
    with open(os.path.join(out_dir, f"{stem}.json"), "w") as f:
        json.dump(creature, f, indent=2)

    sprite = creature.get("sprite")
    if not sprite:
        return
    key = os.path.splitext(sprite)[0]
    for suffix in ("", "_big"):
        src = os.path.join(data_dir, f"{key}{suffix}.png")
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(out_dir, f"{stem}{suffix}.png"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", help="photo file or folder")
    ap.add_argument("-o", "--out", default="creatures")
    ap.add_argument("--data", default=DATA_DIR,
                    help="persistent registry + sprite bank (default: gamedata)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-sprite", action="store_true",
                    help="stage 1 only -- free, useful while out of credits")
    ap.add_argument("--list-models", action="store_true")
    args = ap.parse_args()

    if args.list_models:
        try:
            for m in get_client().models.list():
                print(getattr(m, "name", m))
        except Exception as e:                      # noqa: BLE001
            print(f"couldn't list models: {e}")
        return
    if not args.src:
        ap.error("give me a photo or a folder")

    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.data, exist_ok=True)
    client = get_client()

    paths = []
    if os.path.isdir(args.src):
        paths = [os.path.join(args.src, n) for n in sorted(os.listdir(args.src))
                 if n.lower().endswith((".jpg", ".jpeg", ".png"))]
        if not paths:
            sys.exit(f"no photos in {args.src}")
    else:
        paths = [args.src]

    ok = 0
    for p in paths:
        try:
            process(p, client, args.out, args.data,
                    cache=not args.no_cache, make_sprite=not args.no_sprite)
            ok += 1
        except Exception as e:                      # noqa: BLE001
            print(f"{os.path.basename(p)}: FAILED -- {e}")
    print(f"\n{ok}/{len(paths)} processed. "
          f"data: {os.path.abspath(args.data)}  out: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
