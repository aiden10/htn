from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile, status


app = FastAPI(
    title="Pokémon Generator Server",
    description="Receives Poké Ball images and prepares them for Pokémon generation.",
    version="0.1.0",
)

# Keep uploads outside the Python package so they can later be handed to an AI
# generation worker or replaced by object storage without changing the API.
UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


@app.get("/", tags=["health"])
async def root() -> dict[str, str]:
    """Return a small hint when the server is opened in a browser."""

    return {"service": "pokemon-generator", "upload_endpoint": "POST /pokemon"}


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/pokemon", status_code=status.HTTP_201_CREATED, tags=["pokemon"])
async def create_pokemon(image: UploadFile = File(...)) -> dict[str, object]:
    """Accept a Poké Ball image and save it for the generation pipeline.

    Only returns a confirmation that the image was received right now
    """

    content_type = (image.content_type or "").lower()
    extension = ALLOWED_IMAGE_TYPES.get(content_type)
    if extension is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Upload a JPEG, PNG, or WebP image.",
        )

    image_id = uuid4().hex
    destination = UPLOAD_DIR / f"{image_id}{extension}"
    total_bytes = 0

    try:
        with destination.open("wb") as output:
            while chunk := await image.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > MAX_IMAGE_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Image must be 10 MiB or smaller.",
                    )
                output.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to store uploaded image.",
        ) from exc
    finally:
        await image.close()

    return {
        "image_id": image_id,
        "status": "received",
        "filename": image.filename,
        "content_type": content_type,
        "size_bytes": total_bytes,
        "next_step": "Generate a Pokémon from this image.",
    }
