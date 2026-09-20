"""Helpers for splitting image payloads into raw-byte chunks and processing them asynchronously.

The intended flow is intentionally simple:
- read the image once as raw bytes
- split those bytes into fixed-size chunks
- process each chunk independently in parallel
- merge the processed chunks back together once all work finishes

This is closer to the byte-oriented partitioning pattern you want than a grid crop of PIL tiles.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import math
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any, TypeVar

T = TypeVar("T")


def _get_pillow_image_class():
    """Return PIL.Image if Pillow is available; otherwise return None."""
    if importlib.util.find_spec("PIL") is None:
        return None
    return importlib.import_module("PIL").Image


def partition(items: Sequence[T], partition_size: int) -> tuple[list[list[int]], list[int]]:
    """Split a sequence using the same full-batch + remainder pattern used elsewhere."""
    if partition_size <= 0:
        raise ValueError("partition_size must be greater than 0")

    length = len(items)
    if length == 0:
        return [], []

    full_batches: list[list[int]] = []
    lines = math.floor(length / partition_size)
    remaining_lines = length - (lines * partition_size)
    dimensions = lines * partition_size

    if dimensions > 0:
        start = 0
        while start < dimensions:
            stop = min(start + partition_size, dimensions)
            full_batches.append(list(range(start, stop)))
            start = stop

    remainder = list(range(dimensions, length)) if remaining_lines != 0 else []
    return full_batches, remainder


def ensure_image_bytes(image: Any) -> bytes:
    """Return raw bytes from a file path, file-like object, or raw byte payload."""
    if image is None:
        raise ValueError("image cannot be None")

    if isinstance(image, (bytes, bytearray)):
        return bytes(image)

    if hasattr(image, "read"):
        data = image.read()
        return bytes(data) if isinstance(data, (bytes, bytearray)) else data

    if isinstance(image, str):
        with open(image, "rb") as image_file:
            return image_file.read()

    raise TypeError(f"Unsupported image type: {type(image)!r}")


def encode_image_to_base64(image: Any) -> str:
    """Encode raw image bytes to a base64 string so they can be sent to Ollama."""
    return base64.b64encode(ensure_image_bytes(image)).decode("utf-8")


def partition_image_bytes(image: Any, chunk_size: int = 64 * 1024) -> list[bytes]:
    """Split an image into smaller raw-byte chunks.

    This is the preferred image-partitioning strategy for async work because it preserves the
    original byte stream and avoids cropping the image into a grid of tiles.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")

    payload = ensure_image_bytes(image)
    if not payload:
        return []

    return [payload[index : index + chunk_size] for index in range(0, len(payload), chunk_size)]


def merge_image_chunks(chunks: Iterable[bytes | bytearray | memoryview]) -> bytes:
    """Join processed byte chunks back into a single byte stream."""
    merged = bytearray()
    for chunk in chunks:
        if chunk is None:
            continue
        merged.extend(bytes(chunk))
    return bytes(merged)


async def process_image_chunks_async(
    chunks: Iterable[bytes | bytearray | memoryview],
    processor: Callable[[bytes], bytes | bytearray | Awaitable[bytes | bytearray]],
    *,
    max_concurrency: int = 4,
) -> bytes:
    """Process each byte chunk in parallel and merge the results back into one payload."""
    chunk_list = [bytes(chunk) for chunk in chunks]
    if not chunk_list:
        return b""

    semaphore = asyncio.Semaphore(max_concurrency)

    async def _process_one(chunk: bytes) -> bytes:
        async with semaphore:
            result = processor(chunk)
            if asyncio.iscoroutine(result):
                return bytes(await result)
            return bytes(result)

    processed_chunks = await asyncio.gather(*(_process_one(chunk) for chunk in chunk_list))
    return merge_image_chunks(processed_chunks)


async def process_image_bytes_async(
    image: Any,
    processor: Callable[[bytes], bytes | bytearray | Awaitable[bytes | bytearray]],
    *,
    chunk_size: int = 64 * 1024,
    max_concurrency: int = 4,
) -> bytes:
    """Convenience wrapper that reads an image, chunks its raw bytes, and merges the results."""
    chunks = partition_image_bytes(image, chunk_size=chunk_size)
    return await process_image_chunks_async(chunks, processor, max_concurrency=max_concurrency)


def partition_image(image: Any, max_width: int = 1024, max_height: int = 1024) -> list[Any]:
    """Legacy compatibility helper.

    This older implementation crops the image into a tile grid. It is still useful when a caller
    specifically wants separate image tiles, but the byte-partitioning helpers above are the
    preferred solution when the goal is speed and raw-byte reassembly.
    """
    image_class = _get_pillow_image_class()
    if image_class is None:
        raise RuntimeError("Pillow is required for image tiling. Install it with: pip install pillow")

    image_bytes = ensure_image_bytes(image)
    opened_image = image_class.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = opened_image.size

    cols = max(1, math.ceil(width / max_width))
    rows = max(1, math.ceil(height / max_height))

    tiles: list[Any] = []
    tile_width = width // cols
    tile_height = height // rows

    for row_index in range(rows):
        for col_index in range(cols):
            left = col_index * tile_width
            top = row_index * tile_height
            right = left + tile_width if col_index < cols - 1 else width
            bottom = top + tile_height if row_index < rows - 1 else height
            tiles.append(opened_image.crop((left, top, right, bottom)))

    return tiles


def partition_image_for_ollama(
    image: Any,
    max_width: int = 1024,
    max_height: int = 1024,
    partition_size: int = 4,
) -> list[list[str]]:
    """Tile one image and return base64 image payloads grouped by batch size."""
    tiles = partition_image(image, max_width=max_width, max_height=max_height)
    encoded_tiles = [base64.b64encode(tile_bytes).decode("utf-8") for tile_bytes in _tile_to_bytes(tiles)]

    main_batches, remainder = partition(encoded_tiles, partition_size)
    batches: list[list[str]] = []

    for batch in main_batches:
        batches.append([encoded_tiles[index] for index in batch])

    if remainder:
        batches.append([encoded_tiles[index] for index in remainder])

    return batches


def _tile_to_bytes(tiles: Iterable[Any]) -> list[bytes]:
    """Convert tiles to in-memory PNG bytes for base64 encoding."""
    image_bytes: list[bytes] = []
    for tile in tiles:
        buffer = io.BytesIO()
        tile.save(buffer, format="PNG")
        image_bytes.append(buffer.getvalue())
    return image_bytes


def process_single_image_with_ollama(
    image: Any,
    prompt: str = "Extract the readable text from this image. If there is no readable text, return exactly: NO_TEXT.",
    model: str = "llava:7b",
    *,
    chunk_size: int = 64 * 1024,
) -> str:
    """Return the text found inside a single image, if any.

    The workflow follows the raw-byte partitioning utilities that were added for image processing:
    - normalize the image payload into raw bytes
    - split those bytes into chunked byte streams for the same canonical validation path used elsewhere
    - convert the normalized payload to base64 once for Ollama
    - ask Ollama to decide whether readable text is present
    - if it is present, return the extracted text; otherwise return an empty string
    """
    try:
        import ollama
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("ollama package is required for image processing.") from exc

    raw_image = merge_image_chunks(partition_image_bytes(image, chunk_size=chunk_size))
    if not raw_image:
        return ""

    encoded_image = encode_image_to_base64(raw_image)
    client = ollama.Client()

    response = client.chat(
        model=model,
        messages=[
            {
                "role": "user",
                "content": prompt,
                "images": [encoded_image],
            }
        ],
    )

    content = str(response["message"]["content"]).strip()
    if not content:
        return ""

    normalized = content.lower()
    if any(marker in normalized for marker in ("no_text", "no text", "no readable text", "no text detected")):
        return ""

    return content
