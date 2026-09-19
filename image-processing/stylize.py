"""
stylize.py -- turns whatever the image model returns into a consistent sprite.

This is the part that makes fifty independently-generated creatures look like
they came from one game. The model handles *content*; this handles *style*.

    pip install pillow numpy
    python stylize.py input.png -o out.png
    python stylize.py raw_sprites/ -o stylized/        # whole folder

Pipeline:
    1. square the canvas
    2. knock out the background (chroma key on the colour you prompted for)
    3. downscale to SPRITE_SIZE with a box filter
    4. quantize to a FIXED palette, dithering off   <-- the important step
    5. add a 1px dark outline
    6. nearest-neighbour upscale for display

Steps 4 and 5 are what enforce a house style. Everything else is cleanup.
"""

import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageFilter

# --------------------------------------------------------------- settings ---

SPRITE_SIZE = 64  # the real resolution of the sprite
DISPLAY_SCALE = 8  # 64 * 8 = 512px for the web page
BG_KEY = (255, 0, 255)  # the background colour you ask the model for
BG_TOLERANCE = 90  # how far from BG_KEY still counts as background
ALPHA_CUTOFF = 128  # binarise alpha; sprites have hard edges, not soft
OUTLINE_COLOUR = (26, 20, 35)

# A fixed 24-colour palette. Every creature is forced into these colours, which
# is why they end up looking like a set. Swap these for your own art direction
# -- just keep the count low and the ramps consistent.
PALETTE = [
    (26, 20, 35),
    (58, 44, 64),
    (94, 66, 80),
    (140, 94, 90),  # darks / skin ramp
    (186, 130, 94),
    (224, 178, 128),
    (247, 222, 180),  # light ramp
    (36, 66, 74),
    (48, 112, 108),
    (78, 168, 136),
    (140, 212, 150),  # greens
    (28, 52, 96),
    (44, 96, 156),
    (78, 152, 208),
    (150, 208, 236),  # blues
    (96, 32, 56),
    (162, 48, 62),
    (214, 92, 70),
    (244, 152, 84),  # reds / oranges
    (238, 206, 88),
    (120, 60, 140),
    (176, 104, 196),  # yellow / purple
    (128, 128, 136),
    (232, 236, 240),  # neutrals
]

# ------------------------------------------------------------------ steps ---


def square_canvas(img):
    """Pad to a square so the sprite never gets squashed."""
    w, h = img.size
    if w == h:
        return img
    side = max(w, h)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - w) // 2, (side - h) // 2))
    return canvas


def key_out_background(img, key=BG_KEY, tol=BG_TOLERANCE):
    """Chroma key. Far more reliable in a hackathon than a segmentation model,
    provided you prompt for a solid background colour in the first place."""
    img = img.convert("RGBA")
    arr = np.array(img).astype(np.int16)
    dist = np.sqrt(((arr[:, :, :3] - np.array(key)) ** 2).sum(axis=2))
    arr[:, :, 3] = np.where(dist < tol, 0, arr[:, :, 3])
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def trim_to_content(img, margin=2):
    """Crop to the creature, then re-square. Keeps sprites consistently sized
    in frame no matter how the model composed the image."""
    bbox = img.getchannel("A").getbbox()
    if not bbox:
        return img
    l, t, r, b = bbox
    l, t = max(0, l - margin), max(0, t - margin)
    r, b = min(img.width, r + margin), min(img.height, b + margin)
    return square_canvas(img.crop((l, t, r, b)))


def palette_image(colours=PALETTE):
    flat = [c for rgb in colours for c in rgb]
    flat += [0] * (768 - len(flat))
    pal = Image.new("P", (1, 1))
    pal.putpalette(flat)
    return pal


def quantize_to_palette(img, colours=PALETTE):
    """Force every pixel into the fixed palette. Dithering OFF -- dithering is
    what makes AI 'pixel art' look like noise instead of art."""
    alpha = img.getchannel("A").point(lambda a: 255 if a >= ALPHA_CUTOFF else 0)
    rgb = img.convert("RGB")
    quantized = rgb.quantize(
        palette=palette_image(colours), dither=Image.Dither.NONE
    ).convert("RGB")
    out = quantized.convert("RGBA")
    out.putalpha(alpha)
    return out


def add_outline(img, colour=OUTLINE_COLOUR):
    """Dilate the silhouette by a pixel and paint the ring dark. A shared
    outline unifies sprites more than almost anything else you can do."""
    alpha = img.getchannel("A")
    grown = alpha.filter(ImageFilter.MaxFilter(3))
    ring = np.array(grown).astype(np.int16) - np.array(alpha).astype(np.int16)
    ring = ring > 0

    arr = np.array(img)
    arr[ring] = [*colour, 255]
    return Image.fromarray(arr, "RGBA")


# -------------------------------------------------------------- pipeline ---


def stylize(img, size=SPRITE_SIZE, scale=DISPLAY_SCALE, key_bg=True):
    img = img.convert("RGBA")
    img = square_canvas(img)
    if key_bg:
        img = key_out_background(img)
        img = trim_to_content(img)
    img = img.resize((size, size), Image.BOX)
    img = quantize_to_palette(img)
    img = add_outline(img)
    small = img
    big = img.resize((size * scale, size * scale), Image.NEAREST)
    return small, big


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="image file or folder")
    ap.add_argument("-o", "--out", required=True, help="output file or folder")
    ap.add_argument("--size", type=int, default=SPRITE_SIZE)
    ap.add_argument("--scale", type=int, default=DISPLAY_SCALE)
    ap.add_argument(
        "--no-key",
        action="store_true",
        help="skip background removal (for testing on photos)",
    )
    args = ap.parse_args()

    if os.path.isdir(args.src):
        os.makedirs(args.out, exist_ok=True)
        names = [
            n
            for n in sorted(os.listdir(args.src))
            if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
        ]
        if not names:
            sys.exit(f"no images found in {args.src}")
        for n in names:
            small, big = stylize(
                Image.open(os.path.join(args.src, n)),
                args.size,
                args.scale,
                not args.no_key,
            )
            stem = os.path.splitext(n)[0]
            small.save(os.path.join(args.out, f"{stem}_{args.size}.png"))
            big.save(os.path.join(args.out, f"{stem}_big.png"))
            print(f"{n} -> {stem}_{args.size}.png, {stem}_big.png")
    else:
        small, big = stylize(
            Image.open(args.src), args.size, args.scale, not args.no_key
        )
        big.save(args.out)
        small.save(os.path.splitext(args.out)[0] + f"_{args.size}.png")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
