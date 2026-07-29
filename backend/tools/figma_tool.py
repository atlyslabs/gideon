"""
Figma Tools -- interact with Figma designs via Figma REST API v1.

API docs: https://developers.figma.com/docs/rest-api/
Auth: Bearer token (OAuth access_token).
"""

import os
import re
from typing import Any

import httpx
import structlog

from tools.base import BaseTool

logger = structlog.get_logger()

FIGMA_API_URL = "https://api.figma.com/v1"

# Your Figma team id, the number in https://figma.com/files/team/<id>/...
# Only needed by list_figma_team_projects; every other Figma tool works from a
# file URL and needs nothing here.
FIGMA_TEAM_ID = os.environ.get("FIGMA_TEAM_ID", "")


def _parse_figma_url(url: str) -> tuple[str, str | None]:
    """Extract file_key and node_id from a Figma URL.

    Returns (file_key, node_id) where node_id may be None.
    """
    # https://www.figma.com/design/ABC123/Title?node-id=1-2
    # https://www.figma.com/file/ABC123/Title?node-id=1%3A2
    match = re.search(r"figma\.com/(?:design|file|proto)/([a-zA-Z0-9]+)", url)
    if not match:
        raise ValueError(f"Invalid Figma URL: could not extract file key from '{url[:100]}'")
    file_key = match.group(1)

    node_match = re.search(r"node-id=([^&]+)", url)
    node_id = None
    if node_match:
        node_id = node_match.group(1).replace("%3A", ":").replace("-", ":")
    return file_key, node_id


async def _figma_get(access_token: str, path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"{FIGMA_API_URL}{path}",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            params=params,
        )

    if response.status_code == 401:
        raise Exception("401: Figma token expired or invalid")
    if response.status_code == 403:
        return {"error": "Figma access denied. You may not have access to this file."}
    if response.status_code == 404:
        return {"error": "Figma file or node not found."}
    if response.status_code != 200:
        logger.error("Figma API error", status=response.status_code, path=path)
        return {"error": f"Figma API error: {response.status_code}"}

    return response.json()


def _summarize_node(node: dict, depth: int = 0, max_depth: int = 4) -> dict:
    """Recursively summarize a Figma node tree, keeping it concise."""
    summary: dict[str, Any] = {
        "name": node.get("name"),
        "type": node.get("type"),
    }

    if node.get("absoluteBoundingBox"):
        bb = node["absoluteBoundingBox"]
        summary["size"] = f"{bb.get('width', 0):.0f}x{bb.get('height', 0):.0f}"

    if node.get("fills"):
        colors = []
        for fill in node["fills"]:
            if fill.get("type") == "SOLID" and fill.get("color"):
                c = fill["color"]
                r, g, b = int(c.get("r", 0) * 255), int(c.get("g", 0) * 255), int(c.get("b", 0) * 255)
                colors.append(f"#{r:02x}{g:02x}{b:02x}")
        if colors:
            summary["fills"] = colors

    if node.get("style"):
        summary["text_style"] = {
            "font": node["style"].get("fontFamily"),
            "size": node["style"].get("fontSize"),
            "weight": node["style"].get("fontWeight"),
        }

    if node.get("characters"):
        text = node["characters"]
        summary["text"] = text[:200] if len(text) > 200 else text

    if node.get("layoutMode"):
        summary["layout"] = {
            "mode": node["layoutMode"],
            "spacing": node.get("itemSpacing"),
            "padding": {
                "top": node.get("paddingTop"),
                "right": node.get("paddingRight"),
                "bottom": node.get("paddingBottom"),
                "left": node.get("paddingLeft"),
            },
        }

    if node.get("cornerRadius"):
        summary["corner_radius"] = node["cornerRadius"]

    if node.get("strokes"):
        summary["has_strokes"] = True

    if node.get("effects"):
        summary["effects"] = [e.get("type") for e in node["effects"]]

    children = node.get("children", [])
    if children and depth < max_depth:
        summary["children"] = [_summarize_node(c, depth + 1, max_depth) for c in children[:30]]
    elif children:
        summary["child_count"] = len(children)

    return summary


def _format_component(comp: dict) -> dict:
    return {
        "key": comp.get("key"),
        "name": comp.get("name"),
        "description": comp.get("description"),
        "containing_frame": comp.get("containing_frame", {}).get("name"),
    }


def _format_style(style: dict) -> dict:
    return {
        "key": style.get("key"),
        "name": style.get("name"),
        "style_type": style.get("style_type"),
        "description": style.get("description"),
    }


# ---------------------------------------------------------------------------
# Tool classes
# ---------------------------------------------------------------------------


class GetFigmaDesignContext(BaseTool):
    @property
    def name(self) -> str:
        return "get_figma_design_context"

    @property
    def description(self) -> str:
        return "Get the design context (layout, components, styles, colors, text) for a Figma file or specific node. Provide a Figma URL. Returns a structured summary of the design tree."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_url": {
                    "type": "string",
                    "description": "Figma file or node URL (e.g. https://www.figma.com/design/abc123/...?node-id=1-2)",
                },
                "depth": {
                    "type": "integer",
                    "description": "How deep to traverse the node tree (default 3, max 5)",
                },
            },
            "required": ["file_url"],
        }

    @property
    def provider(self) -> str:
        return "figma"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        file_key, node_id = _parse_figma_url(params["file_url"])
        max_depth = min(params.get("depth", 3), 5)

        api_params = {}
        if node_id:
            api_params["ids"] = node_id

        data = await _figma_get(access_token, f"/files/{file_key}/nodes" if node_id else f"/files/{file_key}", api_params)
        if "error" in data:
            return data

        if node_id and "nodes" in data:
            node_data = data["nodes"].get(node_id, {})
            document = node_data.get("document", {})
        else:
            document = data.get("document", {})

        return {
            "file_name": data.get("name"),
            "design_tree": _summarize_node(document, max_depth=max_depth),
        }


class GetFigmaVariables(BaseTool):
    @property
    def name(self) -> str:
        return "get_figma_variables"

    @property
    def description(self) -> str:
        return "Get published components and styles (colors, typography, etc.) from a Figma file. Returns design system tokens."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_url": {
                    "type": "string",
                    "description": "Figma file URL",
                },
            },
            "required": ["file_url"],
        }

    @property
    def provider(self) -> str:
        return "figma"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        file_key, _ = _parse_figma_url(params["file_url"])
        data = await _figma_get(access_token, f"/files/{file_key}/styles")
        if "error" in data:
            return data

        styles = data.get("meta", {}).get("styles", [])
        return {
            "styles": [_format_style(s) for s in styles],
            "count": len(styles),
        }


class SearchFigmaDesignSystem(BaseTool):
    @property
    def name(self) -> str:
        return "search_figma_design_system"

    @property
    def description(self) -> str:
        return "List all published components in a Figma file. Use to find reusable design system components."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_url": {
                    "type": "string",
                    "description": "Figma file URL to search within",
                },
                "query": {
                    "type": "string",
                    "description": "Optional filter by component name",
                },
            },
            "required": ["file_url"],
        }

    @property
    def provider(self) -> str:
        return "figma"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        file_key, _ = _parse_figma_url(params["file_url"])
        data = await _figma_get(access_token, f"/files/{file_key}/components")
        if "error" in data:
            return data

        components = data.get("meta", {}).get("components", [])
        query = (params.get("query") or "").lower()
        if query:
            components = [c for c in components if query in (c.get("name") or "").lower()]

        return {
            "components": [_format_component(c) for c in components[:50]],
            "count": len(components),
        }


class GetFigmaScreenshot(BaseTool):
    @property
    def name(self) -> str:
        return "get_figma_screenshot"

    @property
    def description(self) -> str:
        return "Get a rendered image URL of a Figma node. Returns a temporary image URL you can share or reference."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_url": {
                    "type": "string",
                    "description": "Figma file or node URL to screenshot",
                },
                "format": {
                    "type": "string",
                    "description": "Image format: png (default), jpg, svg, pdf",
                    "enum": ["png", "jpg", "svg", "pdf"],
                },
                "scale": {
                    "type": "number",
                    "description": "Image scale (0.01 to 4, default 2)",
                },
            },
            "required": ["file_url"],
        }

    @property
    def provider(self) -> str:
        return "figma"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        file_key, node_id = _parse_figma_url(params["file_url"])
        if not node_id:
            return {"error": "A specific node must be selected (URL should contain node-id parameter)."}

        api_params: dict[str, Any] = {
            "ids": node_id,
            "format": params.get("format", "png"),
            "scale": params.get("scale", 2),
        }

        data = await _figma_get(access_token, f"/images/{file_key}", api_params)
        if "error" in data:
            return data

        images = data.get("images", {})
        image_url = images.get(node_id)
        if not image_url:
            return {"error": "Could not render image for this node."}

        return {"image_url": image_url, "node_id": node_id, "format": params.get("format", "png")}


class GetFigmaMetadata(BaseTool):
    @property
    def name(self) -> str:
        return "get_figma_metadata"

    @property
    def description(self) -> str:
        return "Get file-level metadata from a Figma file: name, last modified, version, pages, and top-level node structure."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_url": {
                    "type": "string",
                    "description": "Figma file URL",
                },
            },
            "required": ["file_url"],
        }

    @property
    def provider(self) -> str:
        return "figma"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        file_key, _ = _parse_figma_url(params["file_url"])
        data = await _figma_get(access_token, f"/files/{file_key}", {"depth": 1})
        if "error" in data:
            return data

        pages = []
        for child in data.get("document", {}).get("children", []):
            pages.append({
                "id": child.get("id"),
                "name": child.get("name"),
                "type": child.get("type"),
                "child_count": len(child.get("children", [])),
            })

        return {
            "file_name": data.get("name"),
            "last_modified": data.get("lastModified"),
            "version": data.get("version"),
            "pages": pages,
        }


class ListFigmaTeamProjects(BaseTool):
    @property
    def name(self) -> str:
        return "list_figma_team_projects"

    @property
    def description(self) -> str:
        return "List all projects in the configured Figma team. Use this to discover available projects and then list files within them. No parameters needed."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
        }

    @property
    def provider(self) -> str:
        return "figma"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        if not FIGMA_TEAM_ID:
            return {
                "error": "FIGMA_TEAM_ID is not configured. Set it to your Figma team id "
                "(the number in https://figma.com/files/team/<id>/...)."
            }
        data = await _figma_get(access_token, f"/teams/{FIGMA_TEAM_ID}/projects")
        if "error" in data:
            return data

        projects = data.get("projects", [])
        return {
            "projects": [{"id": p.get("id"), "name": p.get("name")} for p in projects],
            "count": len(projects),
        }


class ListFigmaProjectFiles(BaseTool):
    @property
    def name(self) -> str:
        return "list_figma_project_files"

    @property
    def description(self) -> str:
        return "List all files in a Figma project. Returns file names, URLs, and last modified dates."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Figma project ID. Get it from list_figma_team_projects.",
                },
            },
            "required": ["project_id"],
        }

    @property
    def provider(self) -> str:
        return "figma"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        project_id = params.get("project_id", "").strip()
        if not project_id:
            return {"error": "project_id is required."}

        data = await _figma_get(access_token, f"/projects/{project_id}/files")
        if "error" in data:
            return data

        files = data.get("files", [])
        return {
            "files": [
                {
                    "key": f.get("key"),
                    "name": f.get("name"),
                    "url": f"https://www.figma.com/design/{f.get('key')}",
                    "last_modified": f.get("last_modified"),
                }
                for f in files
            ],
            "count": len(files),
        }
