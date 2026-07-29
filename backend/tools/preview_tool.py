"""
Preview tool — emits a structured payload that the frontend
detects and renders as a live wireframe/React preview in the chat.

The tool itself does NO compilation. It's a "signal" tool: Claude provides
the source code, the tool structures the payload, and the FE component
(WireframePreview.tsx) takes it from there — POSTing the code to a
Next.js API route (`/api/preview/compile`) that runs Vite server-side and
returns compiled HTML/CSS/JS for rendering inside a sandboxed iframe.

Separating the tool (BE-side, instant) from the compile (FE-triggered,
slow) means a chat response with a preview call returns immediately and
the iframe shows a "Compiling…" state while Vite does its thing.

Why this extends InternalTool (not BaseTool):
  `tool_executor.py` routes tools with `provider == "internal"` through
  `InternalTool.execute_internal` and asserts `isinstance(tool, InternalTool)`.
  An empty AssertionError on that branch is what surfaces as the empty
  `error=` log line. Inheriting from InternalTool fits the existing
  pattern — we don't need the user_id/session params but accept and
  ignore them.
"""

import re
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tools.internal_tools import InternalTool

# react-icons/md does NOT use the @mui/icons-material suffix convention.
# Names like `MdDescriptionOutlined` / `MdInfoFilled` / `MdWarningRounded`
# do not exist there and silently fail at Vite compile time — by which
# point we've already returned the preview payload to the FE and the
# error never reaches Claude. We detect these statically so Claude gets a
# structured retry signal inside the same chat turn.
#
# `Outline` (no `d`) IS a valid suffix for *some* icons (e.g.
# `MdHomeOutline`), so we don't ban it — only the four variants below,
# which never exist as react-icons exports.
_INVALID_ICON_SUFFIXES = ("Outlined", "Filled", "Rounded", "Sharp", "TwoTone")
_ICON_IMPORT_RE = re.compile(
    r"""import\s*\{([^}]+)\}\s*from\s*["']react-icons/md["']""",
)
_BAD_ICON_RE = re.compile(
    r"\bMd[A-Z]\w*?(?:" + "|".join(_INVALID_ICON_SUFFIXES) + r")\b",
)


def _invalid_react_icons(code: str) -> list[tuple[str, str]]:
    """Scan `code` for react-icons/md imports that use suffixes the
    library doesn't export. Returns a list of (bad_name, suggested_fix)
    pairs."""
    invalid: list[tuple[str, str]] = []
    for match in _ICON_IMPORT_RE.finditer(code):
        names_blob = match.group(1)
        for raw in names_blob.split(","):
            name = raw.strip().split(" as ")[0].strip()
            if not name:
                continue
            for suffix in _INVALID_ICON_SUFFIXES:
                if name.endswith(suffix) and name != "Md" + suffix:
                    invalid.append((name, name[: -len(suffix)]))
                    break
    return invalid


class GeneratePreview(InternalTool):
    name = "generate_preview"

    description = (
        "Generate a live, interactive UI preview that renders inline in the chat. "
        "Use this for UI mockups, wireframes, page layouts, design sketches, or any "
        "'design / draft / mock up / wireframe this' request. The user sees a real "
        "interactive preview, not just code text.\n\n"
        "**Framework**: prefer `react`. Only fall back to `html` for the simplest "
        "static landing pages or when the user explicitly asks for HTML/CSS.\n\n"
        "**Code rules** (react):\n"
        "- Single file with a single default-exported component.\n"
        "- Do NOT import any CSS files — the harness loads styles automatically.\n"
        "- React 19 + Tailwind v4 are available; use Tailwind utility classes for layout.\n"
        "- Icons: import from `react-icons/md` (unsuffixed names only).\n"
        "- Keep components under 100KB of source; over that the compile rejects it.\n\n"
        "**Code rules** (html):\n"
        "- Self-contained: include `<style>` and inline JS as needed.\n"
        "- Tailwind v4 is NOT available for the html framework — write real CSS.\n\n"
        "Provide a short `description` (one sentence) summarizing what the preview "
        "shows so the user has a label above the iframe."
    )

    input_schema = {
        "type": "object",
        "properties": {
            "framework": {
                "type": "string",
                "enum": ["react", "html"],
                "description": (
                    "Compilation target. `react` is the default and gives you the "
                    "full DS + Tailwind v4 stack. `html` is for static pages "
                    "where you control all the CSS yourself."
                ),
            },
            "code": {
                "type": "string",
                "description": (
                    "Full source. For react: a single .tsx file with a default "
                    "exported component. For html: a complete <!DOCTYPE html>… "
                    "document or a body fragment (the harness wraps it)."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "One-sentence label shown above the preview, e.g. "
                    "'Settings page with toggles for notification preferences'."
                ),
            },
        },
        "required": ["framework", "code"],
    }

    # `provider = "internal"` is inherited from InternalTool — no external
    # integration, executor routes through execute_internal below.

    async def execute_internal(
        self,
        params: dict[str, Any],
        user_id: UUID,  # noqa: ARG002 — unused; pass-through tool has no per-user state
        session: AsyncSession,  # noqa: ARG002 — unused; no DB writes
        **kwargs: Any,
    ) -> dict[str, Any]:
        framework = params.get("framework", "react")
        code = params.get("code", "")
        description = params.get("description", "")

        # Source-size guard mirrors the FE compile route's hard limit so Claude
        # gets feedback at tool-call time rather than after the compile pass.
        if len(code) > 100_000:
            return {
                "error": (
                    "Preview source exceeds 100KB. Trim the component — split "
                    "subcomponents into separate compositions in the same file, "
                    "or simplify the mockup."
                ),
                "source_bytes": len(code),
            }

        if not code.strip():
            return {"error": "Preview source is empty. Provide the component code."}

        # Static pre-flight on react-icons/md imports. Catches the common
        # @mui-style suffix hallucination (Outlined / Filled / Rounded /
        # Sharp / TwoTone) before the FE compile fails — that failure path
        # doesn't surface back to Claude, so we'd otherwise loop on it.
        if framework == "react":
            bad = _invalid_react_icons(code)
            if bad:
                fixes = ", ".join(f"`{b}` → `{f}`" for b, f in bad)
                return {
                    "error": (
                        f"Invalid `react-icons/md` imports: {fixes}. "
                        "The library does not use the `Outlined` / `Filled` / "
                        "`Rounded` / `Sharp` / `TwoTone` suffixes (those are "
                        "@mui/icons-material's convention). Material's default "
                        "filled style IS the unsuffixed name. Reissue "
                        "`generate_preview` with the corrected import."
                    ),
                    "invalid_icons": [
                        {"bad": b, "suggested": f} for b, f in bad
                    ],
                }

        # The actual compile happens FE-side (the frontend calls
        # /api/preview/compile). All we do here is structure the payload the
        # frontend recognizes via `tool_name === "generate_preview"`.
        return {
            "type": "preview",
            "framework": framework,
            "code": code,
            "description": description,
        }
