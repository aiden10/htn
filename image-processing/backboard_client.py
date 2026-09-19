#!/usr/bin/env python3
"""
backboard_client.py -- sprite generation via Backboard's stateless image API.

Raw HTTP on purpose: the docs note `operation` needs a backend and SDK version
that exposes it, and installed pip packages may lag the SDK source.

    pip install requests pillow
    export BACKBOARD_API_KEY=...

    python backboard_client.py --check
    python backboard_client.py "a squat ceramic creature with a handle arm"
    python backboard_client.py "..." --model google/gemini-3.1-flash-image
    python backboard_client.py "..." --raw          # skip the style template

IMPORTANT: by default the prompt you pass is wrapped in STYLE_PROMPT from
generate.py -- the same template the real pipeline uses, magenta background
and all. That's what makes the output testable with stylize.py. Use --raw
only to test the API itself, never to judge sprite quality.

Wire into generate.py by replacing the body of render():

    from backboard_client import generate_sprite
    def render(client, sprite_prompt):
        return generate_sprite(sprite_prompt)      # styling applied inside
"""

import argparse
import io
import json
import os
import re
import sys
import time

import requests
from PIL import Image

BASE_URL = "https://app.backboard.io/api"
MESSAGES = f"{BASE_URL}/threads/messages"

IMAGE_PROVIDER = "openrouter"          # stateless generation supports openrouter
IMAGE_MODEL = "google/gemini-3.1-flash-lite-image"   # half the price of flash
VISION_PROVIDER = "openrouter"
VISION_MODEL = "google/gemini-3-flash"

# Long enough for a slow generation, short enough that a hung call doesn't eat
# the demo. A timeout here does NOT cancel the upstream job -- never retry.
IMAGE_TIMEOUT = 90
VISION_TIMEOUT = 45

# $ per 1M output tokens, for estimating spend when cost_usd comes back null.
PRICES = {
    "google/gemini-3-pro-image": 12.0,
    "google/gemini-3.1-flash-image": 3.0,
    "google/gemini-3.1-flash-lite-image": 1.5,
    "openai/gpt-5-image": 10.0,
    "openai/gpt-5-image-mini": 2.0,
    "openai/gpt-5.4-image-2": 15.0,
}

# Guard against a loop bug quietly draining a topped-up balance. Resets per
# process, which is what you want: stops runaways without nagging across runs.
SPEND_CAP_USD = 5.0
_spent = 0.0


def _headers(json_body=True):
    key = os.environ.get("BACKBOARD_API_KEY")
    if not key:
        sys.exit("BACKBOARD_API_KEY is not set.")
    h = {"X-API-Key": key}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def style_prompt():
    """Single source of truth -- the template lives in generate.py.

    Imported lazily because generate.py imports this module; a top-level
    import here would be circular.
    """
    try:
        from generate import STYLE_PROMPT
        return STYLE_PROMPT
    except ImportError as e:
        raise RuntimeError(
            "couldn't import STYLE_PROMPT from generate.py -- run this from "
            "the folder containing generate.py, or pass --raw"
        ) from e


def _account(model, data):
    """Report tokens and spend; raise before blowing past the cap."""
    global _spent

    inp = data.get("input_tokens")
    out = data.get("output_tokens")
    cost = data.get("cost_usd")

    if cost is None and out:
        cost = out * PRICES.get(model, 0.0) / 1_000_000

    bits = []
    if inp is not None or out is not None:
        bits.append(f"tokens in={inp} out={out}")
    if cost:
        bits.append(f"${cost:.4f}")
    if bits:
        print("  [spend] " + "  ".join(bits), end="")

    if cost:
        _spent += cost
        print(f"  session ${_spent:.2f}")
        if _spent > SPEND_CAP_USD:
            raise RuntimeError(
                f"spend cap hit (${_spent:.2f} > ${SPEND_CAP_USD:.2f}). "
                "Raise SPEND_CAP_USD deliberately if this is expected."
            )
    elif bits:
        print()
    return cost


# ------------------------------------------------------------- generation ---


def generate_sprite(desc, model=IMAGE_MODEL, resolution="1K",
                    aspect_ratio="1:1", seed=None, styled=True,
                    timeout=IMAGE_TIMEOUT):
    """Stateless text-to-image. No chat model rewrites the prompt.

    `desc` is the creature's visual description; the fixed art direction is
    added here unless styled=False.

    NEVER call this in a retry loop. A timed-out generation may still be
    running upstream and is still billed -- retrying pays twice.

    Note: image_config rejects unknown keys. 'background' and 'output_format'
    are NOT in this endpoint's schema, so transparent output isn't available;
    background removal happens via the magenta chroma key in stylize.py.
    """
    prompt = style_prompt().format(desc=desc) if styled else desc

    image_config = {"resolution": resolution, "aspect_ratio": aspect_ratio}
    if seed is not None:
        image_config["seed"] = int(seed)

    body = {
        "content": prompt,
        "operation": "generate_image",
        "image_model_provider": IMAGE_PROVIDER,
        "image_model_name": model,
        "image_config": image_config,
    }

    r = requests.post(MESSAGES, headers=_headers(), json=body, timeout=timeout)
    if r.status_code == 402:
        raise RuntimeError("Backboard credits exhausted (402). Top up.")
    if r.status_code >= 400:
        raise RuntimeError(f"Backboard {r.status_code}: {r.text[:300]}")

    data = r.json()

    # The stateless image response doesn't seem to carry cost_usd/output_tokens
    # where _account looks for them, which leaves the spend cap inert. Run once
    # with BACKBOARD_DEBUG=1 to find where usage actually lives.
    if os.environ.get("BACKBOARD_DEBUG"):
        print("  [debug] response keys:", sorted(data.keys()))
        for k in ("usage", "cost_usd", "input_tokens", "output_tokens",
                  "total_tokens", "model_name", "status"):
            if k in data:
                print(f"  [debug] {k} = {json.dumps(data[k])[:200]}")

    media = data.get("generated_media") or []
    if not media:
        raise RuntimeError(f"no generated_media in response: {str(data)[:300]}")

    _account(model, data)

    # Use the field; never scrape URLs out of assistant prose.
    img_bytes = requests.get(media[0]["url"], timeout=60).content
    return Image.open(io.BytesIO(img_bytes))


def chat_json(prompt, image_path=None, provider=VISION_PROVIDER,
              model=VISION_MODEL, timeout=VISION_TIMEOUT):
    """Chat call that returns parsed JSON, optionally with an image attached.

    Uses json_output, which is cleaner than stripping markdown fences. It is
    ignored when documents, web search or custom tools are active -- none of
    which we use. Parsing stays tolerant anyway, because "ignored" is a
    silent failure mode.
    """
    form = {
        "content": prompt,
        "llm_provider": provider,
        "model_name": model,
        "json_output": "true",
        "stream": "false",
    }

    if image_path:
        mime = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
        with open(image_path, "rb") as f:
            files = [("files", (os.path.basename(image_path), f.read(), mime))]
        r = requests.post(MESSAGES, headers=_headers(json_body=False),
                          data=form, files=files, timeout=timeout)
    else:
        r = requests.post(MESSAGES, headers=_headers(), json=form, timeout=timeout)

    if r.status_code == 402:
        raise RuntimeError("Backboard credits exhausted (402). Top up.")
    if r.status_code == 429:
        raise RuntimeError("Backboard rate limited (429).")
    if r.status_code >= 400:
        raise RuntimeError(f"Backboard {r.status_code}: {r.text[:300]}")

    data = r.json()
    if data.get("status") == "FAILED":
        raise RuntimeError(f"run failed: {str(data)[:300]}")

    _account(model, data)
    return extract_json(data.get("content"))


def extract_json(text):
    """Tolerant: handles fences and leading prose even with json_output on."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in reply: {text[:200]!r}")
    return json.loads(text[start:end + 1])


# ------------------------------------------------------------ diagnostics ---


def check():
    """List every image model with its price, and show the active settings."""
    try:
        r = requests.get(f"{BASE_URL}/models/image/all",
                         headers=_headers(json_body=False),
                         params={"provider": IMAGE_PROVIDER}, timeout=30)
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("models", payload if isinstance(payload, list) else [])
    except Exception as e:                          # noqa: BLE001
        print(f"couldn't list image models: {e}")
        return

    print(f"{len(rows)} image model(s) on {IMAGE_PROVIDER}:\n")
    for m in rows:
        name = m.get("name", "?")
        out = m.get("output_cost_per_1m_tokens")
        mark = "  <-- active" if name == IMAGE_MODEL else ""
        print(f"  {name:42} out/1M: {out}{mark}")

    print(f"\nactive image model : {IMAGE_MODEL}")
    print(f"active vision model: {VISION_PROVIDER}/{VISION_MODEL}")
    print(f"spend cap          : ${SPEND_CAP_USD:.2f} per process")
    try:
        print(f"style prompt       : {len(style_prompt())} chars (from generate.py)")
    except RuntimeError as e:
        print(f"style prompt       : UNAVAILABLE -- {e}")

    # Vision stage needs a chat model that takes images AND honours json_output.
    try:
        r = requests.get(f"{BASE_URL}/models",
                         headers=_headers(json_body=False),
                         params={"model_type": "llm", "supports_vision": "true",
                                 "supports_json_output": "true", "limit": 20},
                         timeout=30)
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("models", payload if isinstance(payload, list) else [])
        print(f"\n{len(rows)} vision + json_output chat model(s), first 20:")
        for m in rows:
            name = m.get("name", "?")
            mark = "  <-- active" if name == VISION_MODEL else ""
            print(f"  {m.get('provider', '?')}/{name}{mark}")
    except Exception as e:                          # noqa: BLE001
        print(f"\ncouldn't list chat models: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?",
                    help="the creature's visual description (no style words)")
    ap.add_argument("-o", "--out", default="sprite_test.png")
    ap.add_argument("--model", default=IMAGE_MODEL,
                    help="override the image model, for A/B testing")
    ap.add_argument("--resolution", default="1K")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--raw", action="store_true",
                    help="send the prompt bare, skipping the style template "
                         "(tests the API, NOT sprite quality)")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if args.check:
        check()
        return
    if not args.prompt:
        ap.error("give me a description, or use --check")

    if args.raw:
        print("WARNING: --raw skips the style template. The result will have "
              "no magenta background and stylize.py will not key it out.")

    t0 = time.time()
    img = generate_sprite(args.prompt, model=args.model,
                          resolution=args.resolution, seed=args.seed,
                          styled=not args.raw)
    img.save(args.out)
    print(f"{args.out}  {img.size}  mode={img.mode}  "
          f"{args.model}  {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
