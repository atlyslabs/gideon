"""
POC Router — generative UI experiment.

Claude composes a JSON tree from a fixed component catalog. The frontend
renders the tree using real React components. Used for the flight-search
generative UI demo at /poc/generative-ui.
"""

import json
import re

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from config import MODEL_CHAT, get_anthropic_client
from middleware.auth import get_current_user
from services.auth_service import AuthUser

logger = structlog.get_logger()

router = APIRouter()


# Catalog is sent as the system prompt. It defines every component Claude
# is allowed to emit, the props each one accepts, and the tokens it can use.
# Keep this stable so prompt caching kicks in.
FLIGHT_UI_CATALOG = """You are a UI composer for a flight booking results page.

Return EXACTLY ONE JSON object matching the schema below — no prose, no markdown
fences. Use ONLY the listed components. Use ONLY the listed token values.
Never invent props, never invent components.

DESIGN TOKENS
  spacing : xs(4) | sm(8) | md(16) | lg(24) | xl(32)
  radius  : sm(6) | md(10) | lg(14) | pill(999)
  tone    : neutral | primary | success | danger | warning | muted

COMPONENTS

Layout primitives
- Stack
    direction: "row" | "col"            // required
    gap?: spacing                       // default "md"
    align?: "start" | "center" | "end" | "stretch"
    justify?: "start" | "center" | "end" | "between"
    wrap?: boolean
- Card
    padding?: spacing                   // default "md"
    tone?: "surface" | "elevated"       // default "surface"
    radius?: radius                     // default "lg"

Text primitives
- Heading
    level: 1 | 2 | 3
    text: string
- Text
    variant?: "body" | "caption" | "label"
    tone?: tone                         // default "neutral"
    text: string
- Pill
    label: string
    tone?: tone

Flight-specific primitives
- AirportTime                            // one end of a flight leg
    code: string                        // 3-letter IATA
    city: string
    time: string                        // "08:30"
    date?: string                       // "Mon, 15 Dec"
    terminal?: string                   // "T2"
- DurationBar                            // visual leg bar with stop dots
    durationLabel: string               // "9h 35m"
    stops: number                       // 0, 1, 2
    stopLabels?: string[]               // ["DXB 1h 30m"] when stops > 0
- AirlineBadge
    code: string                        // "BA", "AI", "EK"
    name: string                        // "British Airways"
    flightNumber: string                // "BA138"
- PriceTag
    amount: number
    currency: string                    // "INR", "USD"
    perPax?: boolean
    note?: string                       // "1 stop", "Cheapest"
- Button
    label: string
    intent?: "primary" | "secondary" | "ghost"
    actionId: string                    // string id only — frontend resolves

OUTPUT SCHEMA
  Node = { "component": "<name>", "props": { ... }, "children"?: [Node, ...] }

GUIDELINES
- Top-level node must be a Stack (direction "col").
- Each flight option = one Card containing a row Stack with: AirlineBadge on the
  left, AirportTime → DurationBar → AirportTime in the middle, PriceTag + Button
  on the right.
- Generate 4 to 6 realistic flight options matching the user request.
- Include a header row at the top with a Heading and a row of filter Pills
  (e.g. "Cheapest", "Fastest", "Non-stop").
- Use realistic terminals, durations, and IATA codes for the requested route.
- Prices should be plausible for the route in INR unless the user asks otherwise.
"""


class GenerateUIRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000)


class GenerateUIResponse(BaseModel):
    tree: dict
    raw: str


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    return _JSON_FENCE_RE.sub("", text).strip()


@router.post("/generative-ui", response_model=GenerateUIResponse)
async def generate_ui(
    body: GenerateUIRequest,
    auth_user: AuthUser = Depends(get_current_user),
):
    """Take a natural-language flight query and return a UI tree."""
    client = get_anthropic_client()

    try:
        resp = await client.messages.create(
            model=MODEL_CHAT,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": FLIGHT_UI_CATALOG,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": body.prompt}],
        )
    except Exception as e:
        logger.error("Generative UI: Claude call failed", error=str(e))
        raise HTTPException(status_code=502, detail="Model call failed")

    raw = "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )
    cleaned = _strip_fences(raw)

    try:
        tree = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning("Generative UI: bad JSON", error=str(e), raw_head=cleaned[:300])
        raise HTTPException(status_code=502, detail="Model returned invalid JSON")

    if not isinstance(tree, dict) or "component" not in tree:
        raise HTTPException(status_code=502, detail="Model returned an invalid tree")

    return GenerateUIResponse(tree=tree, raw=raw)
