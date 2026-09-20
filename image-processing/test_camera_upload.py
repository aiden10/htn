"""Send a random Internet JPEG through the physical Poké Ball upload route.

Run from ``image-processing`` while FastAPI is running:

    py .\test_camera_upload.py

The ESP32 sends a raw JPEG request body, so this deliberately does the same
instead of using a multipart form upload.
"""

from __future__ import annotations

import argparse
from urllib.request import Request, urlopen


DEFAULT_IMAGE_URL = "https://picsum.photos/640/480.jpg"
DEFAULT_SERVER = "http://127.0.0.1:8080"
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def download_jpeg(url: str) -> bytes:
    """Fetch one small random JPEG without saving it to disk."""

    request = Request(url, headers={"User-Agent": "Shutterdex-camera-smoke-test/1.0"})
    with urlopen(request, timeout=30) as response:  # nosec B310: caller controls test URL
        image = response.read(MAX_IMAGE_BYTES + 1)
        content_type = response.headers.get_content_type().lower()
    if len(image) > MAX_IMAGE_BYTES:
        raise RuntimeError("Sample image exceeded the camera upload limit.")
    if content_type != "image/jpeg" or not image.startswith(b"\xff\xd8"):
        raise RuntimeError(f"Sample URL did not return a JPEG (received {content_type}).")
    return image


def upload(server: str, image: bytes) -> str:
    """Match the ESP32's raw ``POST /upload`` request format."""

    request = Request(
        server.rstrip("/") + "/upload",
        data=image,
        headers={"Content-Type": "image/jpeg", "Content-Length": str(len(image))},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:  # nosec B310: local test endpoint by default
        body = response.read().decode("utf-8", "replace")
        if response.status != 202:
            raise RuntimeError(f"Upload returned HTTP {response.status}: {body}")
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=DEFAULT_SERVER, help="FastAPI base URL")
    parser.add_argument("--image-url", default=DEFAULT_IMAGE_URL, help="JPEG URL to upload")
    args = parser.parse_args()

    image = download_jpeg(args.image_url)
    print(f"Downloaded {len(image):,} bytes; uploading to {args.server.rstrip('/')}/upload …")
    print(upload(args.server, image))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
