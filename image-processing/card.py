#!/usr/bin/env python3
"""
card.py -- render a creature as a 320x240 PNG for the badge screen.

The badge service takes a PNG and converts it to RGB565 itself, so this is
plain Pillow drawing at exactly panel resolution. Send it with fit="none" so
nothing is rescaled: the sprite is pixel art and any resample turns it to mush.

Palette is borrowed from stylize.py so the card and the sprite agree.
"""

import os

from PIL import Image, ImageDraw, ImageFont

import game_data
from stylize import PALETTE

W, H = 320, 240

INK = (247, 222, 180)
DIM = (128, 128, 136)
PANEL = (36, 30, 48)
BG = PALETTE[0]  # (26, 20, 35)

TYPE_COLOUR = {
    "ember": (214, 92, 70),
    "tide": (78, 152, 208),
    "verdant": (78, 168, 136),
    "circuit": (176, 104, 196),
    "stone": (186, 130, 94),
}

RARITY_COLOUR = {
    "common": (128, 128, 136),
    "uncommon": (140, 212, 150),
    "rare": (150, 208, 236),
    "legendary": (238, 206, 88),
}

# Pillow's bundled default has no bold, and the panel is small enough that a
# real face matters. First hit wins; the bundled font is the last resort.
_FONT_CANDIDATES = {
    "bold": [
        "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ],
    "mono": [
        "/usr/share/fonts/liberation-mono-fonts/LiberationMono-Bold.ttf",
        "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "C:/Windows/Fonts/consolab.ttf",
    ],
}

_font_cache = {}


def font(kind, size):
    hit = _font_cache.get((kind, size))
    if hit:
        return hit
    for path in _FONT_CANDIDATES[kind]:
        if os.path.exists(path):
            try:
                hit = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
    else:
        hit = ImageFont.load_default(size=size)
    _font_cache[(kind, size)] = hit
    return hit


# ----------------------------------------------------------------- helpers --


def _fit_text(draw, text, kind, size, max_w, min_size=10):
    """Shrink until it fits. Creature names are model-generated and occasionally
    much longer than 'Tartling'."""
    while size > min_size:
        f = font(kind, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 1
    return font(kind, min_size)


def _chip(draw, xy, text, fill, fg=(26, 20, 35), pad=5, size=11):
    x, y = xy
    f = font("bold", size)
    w = draw.textlength(text, font=f) + pad * 2
    h = size + pad
    draw.rectangle([x, y, x + w, y + h], fill=fill)
    draw.text((x + pad, y + h / 2), text, font=f, fill=fg, anchor="lm")
    return w, h


def _stat_row(draw, x, y, w, label, value, colour, cap=180):
    f = font("mono", 11)
    draw.text((x, y + 6), label, font=f, fill=DIM, anchor="lm")
    bar_x = x + 30
    bar_w = w - 30 - 26
    draw.rectangle([bar_x, y + 1, bar_x + bar_w, y + 11], fill=(52, 44, 68))
    filled = int(bar_w * min(value, cap) / cap)
    if filled > 0:
        draw.rectangle([bar_x, y + 1, bar_x + filled, y + 11], fill=colour)
    draw.text((x + w, y + 6), str(value), font=f, fill=INK, anchor="rm")


def _load_sprite(sprite, data_dir):
    """Accept a PIL Image, an absolute path, or a filename inside data_dir."""
    if sprite is None:
        return None
    if hasattr(sprite, "convert"):
        return sprite.convert("RGBA")
    path = sprite if os.path.isabs(sprite) else os.path.join(data_dir, sprite)
    if not os.path.exists(path):
        return None
    return Image.open(path).convert("RGBA")


# -------------------------------------------------------------------- card --


def render_card(creature, sprite=None, data_dir="gamedata", owner=None):
    """A full creature card at panel resolution. Returns an RGB PIL Image."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    ctype = creature.get("type", "stone")
    accent = TYPE_COLOUR.get(ctype, DIM)
    rarity = creature.get("rarity", "common")

    # --- header -----------------------------------------------------------
    d.rectangle([0, 0, W, 32], fill=PANEL)
    d.rectangle([0, 32, W, 34], fill=accent)

    name = str(creature.get("name", "???")).upper()
    d.text((9, 17), name, font=_fit_text(d, name, "bold", 21, 196),
           fill=INK, anchor="lm")

    _chip(d, (W - 76, 8), rarity.upper()[:9],
          RARITY_COLOUR.get(rarity, DIM), size=11)

    # --- sprite panel -----------------------------------------------------
    # 128 so a 64px sprite lands on an exact 2x, not a 1x rattling in a box.
    px, py, ps = 8, 40, 128
    d.rectangle([px, py, px + ps, py + ps], fill=PANEL, outline=accent)

    art = _load_sprite(sprite, data_dir)
    if art is not None:
        # Integer nearest-neighbour only. A 64px sprite goes to 128 and is
        # cropped by 2px a side rather than resampled to 124.
        scale = max(1, ps // max(art.width, art.height))
        art = art.resize((art.width * scale, art.height * scale), Image.NEAREST)
        if art.width > ps or art.height > ps:  # oversized source, centre-crop
            l, t = (art.width - ps) // 2, (art.height - ps) // 2
            art = art.crop((max(l, 0), max(t, 0),
                            max(l, 0) + min(art.width, ps),
                            max(t, 0) + min(art.height, ps)))
        box = (px + (ps - art.width) // 2, py + (ps - art.height) // 2)
        img.paste(art, box, art)
    else:
        d.text((px + ps / 2, py + ps / 2), "no sprite",
               font=font("mono", 11), fill=DIM, anchor="mm")

    # --- type + stats -----------------------------------------------------
    rx = px + ps + 10
    rw = W - rx - 9

    _chip(d, (rx, 44), ctype.upper(), accent, size=12)

    stats = creature.get("stats", {})
    for i, (label, key) in enumerate(
        [("HP", "hp"), ("ATK", "attack"), ("DEF", "defense"), ("SPD", "speed")]
    ):
        _stat_row(d, rx, 68 + i * 15, rw, label, int(stats.get(key, 0)), accent)

    species = str(creature.get("species", "")).replace("_", " ")
    if species:
        f = _fit_text(d, species, "mono", 10, rw - 26)
        d.text((rx, 141), species, font=f, fill=DIM, anchor="lm")

    sightings = creature.get("sightings")
    if sightings:
        d.text((rx + rw, 141), f"#{sightings}", font=font("mono", 10),
               fill=DIM, anchor="rm")

    # --- moves ------------------------------------------------------------
    d.text((9, 178), "MOVES", font=font("mono", 9), fill=DIM, anchor="lm")
    moves = creature.get("moves", [])[:4]
    for i, mid in enumerate(moves):
        m = game_data.MOVES.get(mid, {"name": mid, "type": ctype, "power": 0})
        cx = 8 + (i % 2) * 154
        cy = 188 + (i // 2) * 22
        d.rectangle([cx, cy, cx + 148, cy + 19], fill=PANEL)
        d.rectangle([cx, cy, cx + 3, cy + 19],
                    fill=TYPE_COLOUR.get(m.get("type"), DIM))
        label = m.get("name", mid)
        f = _fit_text(d, label, "bold", 11, 108)
        d.text((cx + 8, cy + 10), label, font=f, fill=INK, anchor="lm")
        power = m.get("power") or 0
        d.text((cx + 144, cy + 10), str(power) if power else "-",
               font=font("mono", 10), fill=DIM, anchor="rm")

    # --- owner tag --------------------------------------------------------
    if owner:
        d.text((W - 9, 178), owner.upper(), font=font("mono", 9),
               fill=accent, anchor="rm")

    return img


# ------------------------------------------------------------------ status --


def render_status(title, subtitle=None, accent=(153, 69, 255), spinner=None):
    """A between-states screen: 'SQUEEZE TO CATCH', 'LOOKING...', an error.

    `spinner` is an integer frame counter; pass it from a loop for motion.
    """
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, W, 4], fill=accent)
    d.rectangle([0, H - 4, W, H], fill=accent)

    t = str(title).upper()
    d.text((W / 2, H / 2 - 14), t, font=_fit_text(d, t, "bold", 28, W - 40),
           fill=INK, anchor="mm")

    if subtitle:
        s = str(subtitle)
        d.text((W / 2, H / 2 + 18), s,
               font=_fit_text(d, s, "mono", 13, W - 40), fill=DIM, anchor="mm")

    if spinner is not None:
        dots = 1 + spinner % 3
        d.text((W / 2, H - 34), "." * dots, font=font("bold", 24),
               fill=accent, anchor="mm")

    return img


def render_error(message):
    return render_status("JAMMED", message, accent=(214, 92, 70))


# --------------------------------------------------------------------- cli --


def main():
    """Preview cards without a badge: renders every cached creature to PNG."""
    import argparse
    import glob
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="gamedata")
    ap.add_argument("--out", default="/tmp/cards")
    ap.add_argument("--send", metavar="HTN_ID",
                    help="also push the first card to this badge")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    caches = sorted(glob.glob(os.path.join(a.data_dir, "cache_*.json")))
    if not caches:
        print(f"no cache_*.json in {a.data_dir}")
        return

    first = None
    for path in caches:
        with open(path) as f:
            creature = json.load(f)
        img = render_card(creature, creature.get("sprite"), a.data_dir)
        dest = os.path.join(a.out, f"card_{creature.get('name', 'x')}.png")
        img.save(dest)
        first = first or img
        print(dest)

    render_status("Squeeze to catch", "the ball is listening").save(
        os.path.join(a.out, "card_idle.png"))
    print(os.path.join(a.out, "card_idle.png"))

    if a.send and first is not None:
        from badge_api import Badge

        b = Badge(a.send, os.environ["BADGE_KEY"])
        b.image(first, fit="none")
        print(f"sent to {a.send}")


if __name__ == "__main__":
    main()
