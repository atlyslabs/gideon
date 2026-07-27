"""Smoke-test: does the WhatsApp media path's IMAGE block actually reach the
model through our LLM wrapper on the CURRENTLY-CONFIGURED trigger-agent provider?

Prod runs the trigger agent on Kimi (`PROVIDER_TRIGGER_AGENT=kimi` →
`kimi-k2.6`). The WhatsApp router emits an ANTHROPIC-shaped image block
(`{type:image, source:{type:base64,...}}`); the OpenAI-compatible (Kimi) adapter
is supposed to translate that into an `image_url` data URI. This proves the whole
chain end-to-end against the live API: build a solid-color PNG, send it as that
exact block, and check the model reports the color back.

Run (from services/gideon, with KIMI_API_KEY in env — e.g. sourced from .env):
    poetry run python scripts/smoke_whatsapp_media.py
    poetry run python scripts/smoke_whatsapp_media.py --provider anthropic  # defaults to claude-sonnet-5
"""

import argparse
import asyncio
import base64
import struct
import sys
import zlib
from dataclasses import replace
from pathlib import Path

# Make the Gideon project root importable so `poetry run python
# scripts/smoke_whatsapp_media.py` works as documented (matches the sibling
# smoke scripts) — otherwise `services.llm` isn't on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.llm import get_spec, llm  # noqa: E402 (after sys.path setup)

# Provider-native default model, used when --model isn't passed. Picking a Kimi
# id for a non-Kimi provider would fail before validating image handling.
_DEFAULT_MODEL = {
    "kimi": "kimi-k2.6",
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-5.5",
}


def _red_png(size: int = 64) -> bytes:
    """A `size`x`size` solid-red PNG built with stdlib only (no image libraries)."""
    def _chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    row = b"\x00" + (b"\xff\x00\x00" * size)  # filter byte 0 + red RGB pixels
    raw = row * size
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit truecolor RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="kimi", help="kimi | anthropic | openai")
    ap.add_argument("--model", default=None, help="provider-native model id (default: per --provider)")
    args = ap.parse_args()

    model = args.model or _DEFAULT_MODEL.get(args.provider)
    if not model:
        ap.error(f"no default model for provider '{args.provider}' — pass --model")
    spec = replace(get_spec("trigger_agent"), provider=args.provider, model=model)

    # The exact block shape the WhatsApp router builds for an inbound image.
    image_block = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(_red_png()).decode(),
        },
    }
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "What single color fills this image? Reply with just the color word."},
            image_block,
        ],
    }]

    print(f"→ testing vision on {spec.provider}/{spec.model} …")
    try:
        resp = await llm.complete(
            spec, messages, system="You are a vision smoke test.", timeout=60
        )
    except Exception as e:
        print(f"✗ FAILED: the provider rejected the image block: {type(e).__name__}: {str(e)[:300]}")
        return 2

    text = (resp.text or "").strip()
    print(f"  stop_reason={resp.stop_reason!r}  reply={text!r}")
    if "red" in text.lower():
        print("✓ PASS — the model saw the image (vision works through our adapter on this provider).")
        return 0
    print("✗ INCONCLUSIVE — no error, but the reply didn't identify the color. Inspect the reply above.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
