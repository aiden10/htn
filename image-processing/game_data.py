"""
game_data.py -- the fixed rules of the game.

The model NEVER invents a move, a number, or a type. It picks from these
tables. That is what makes battles fair, what stops a judge breaking it in
ten seconds, and what makes the randomness fun instead of arbitrary.

Aiden imports this too -- it's the shared vocabulary between generation and
battle resolution.
"""

STAT_BUDGET = 400  # every creature gets exactly this, distributed
STAT_MIN = 40  # no stat may fall below this
STAT_MAX = 180  # or rise above this
MOVES_PER_CREATURE = 4

TYPES = ["ember", "tide", "verdant", "circuit", "stone"]

# Rock-paper-scissors, extended. attacker -> defender -> multiplier.
# Deliberately small: five types is enough to feel strategic and small
# enough that a judge can understand it during a 90-second demo.
_STRONG_AGAINST = {
    "ember": ["verdant", "circuit"],
    "tide": ["ember", "stone"],
    "verdant": ["tide", "stone"],
    "circuit": ["tide", "verdant"],
    "stone": ["ember", "circuit"],
}


def type_multiplier(attack_type, defend_type):
    if defend_type in _STRONG_AGAINST.get(attack_type, []):
        return 2.0
    if attack_type in _STRONG_AGAINST.get(defend_type, []):
        return 0.5
    return 1.0


# effect is resolved by the battle engine; keep the set small and obvious.
#   None          -- plain damage
#   "heal"        -- heal the user for half the damage dealt
#   "lower_atk"   -- drop the target's attack a stage
#   "lower_def"   -- drop the target's defense a stage
#   "raise_atk"   -- raise the user's attack a stage
#   "priority"    -- always moves first
#   "recoil"      -- user takes a quarter of the damage dealt
MOVES = {
    # ember
    "cinder_flick": {
        "name": "Cinder Flick",
        "type": "ember",
        "power": 40,
        "effect": "priority",
    },
    "scorch_wave": {
        "name": "Scorch Wave",
        "type": "ember",
        "power": 75,
        "effect": None,
    },
    "flare_charge": {
        "name": "Flare Charge",
        "type": "ember",
        "power": 100,
        "effect": "recoil",
    },
    "heat_haze": {
        "name": "Heat Haze",
        "type": "ember",
        "power": 50,
        "effect": "lower_def",
    },
    # tide
    "drizzle_jab": {
        "name": "Drizzle Jab",
        "type": "tide",
        "power": 45,
        "effect": "priority",
    },
    "undertow": {
        "name": "Undertow",
        "type": "tide",
        "power": 70,
        "effect": "lower_atk",
    },
    "tidal_slam": {"name": "Tidal Slam", "type": "tide", "power": 95, "effect": None},
    "rinse": {"name": "Rinse", "type": "tide", "power": 55, "effect": "heal"},
    # verdant
    "thorn_tap": {
        "name": "Thorn Tap",
        "type": "verdant",
        "power": 40,
        "effect": "priority",
    },
    "root_bind": {
        "name": "Root Bind",
        "type": "verdant",
        "power": 60,
        "effect": "lower_atk",
    },
    "bloom_burst": {
        "name": "Bloom Burst",
        "type": "verdant",
        "power": 90,
        "effect": None,
    },
    "photosynth": {
        "name": "Photosynth",
        "type": "verdant",
        "power": 50,
        "effect": "heal",
    },
    # circuit
    "static_nip": {
        "name": "Static Nip",
        "type": "circuit",
        "power": 45,
        "effect": "priority",
    },
    "overclock": {
        "name": "Overclock",
        "type": "circuit",
        "power": 55,
        "effect": "raise_atk",
    },
    "arc_lash": {"name": "Arc Lash", "type": "circuit", "power": 85, "effect": None},
    "short_circuit": {
        "name": "Short Circuit",
        "type": "circuit",
        "power": 105,
        "effect": "recoil",
    },
    # stone
    "pebble_toss": {
        "name": "Pebble Toss",
        "type": "stone",
        "power": 40,
        "effect": "priority",
    },
    "grind_down": {
        "name": "Grind Down",
        "type": "stone",
        "power": 65,
        "effect": "lower_def",
    },
    "boulder_drop": {
        "name": "Boulder Drop",
        "type": "stone",
        "power": 100,
        "effect": None,
    },
    "bedrock_stance": {
        "name": "Bedrock Stance",
        "type": "stone",
        "power": 50,
        "effect": "raise_atk",
    },
}

RARITIES = ["common", "uncommon", "rare", "legendary"]


def moves_of_type(t):
    return [k for k, v in MOVES.items() if v["type"] == t]


def move_menu_for_prompt():
    """Compact listing to paste into the vision prompt."""
    lines = []
    for t in TYPES:
        ids = moves_of_type(t)
        lines.append(f"  {t}: {', '.join(ids)}")
    return "\n".join(lines)
