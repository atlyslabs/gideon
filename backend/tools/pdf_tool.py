"""
PDF Tools — markdown_to_pdf and html_to_pdf.

Both tools share one renderer (WeasyPrint). Markdown is converted to HTML
first, then rendered the same way HTML input is. The output bytes are
uploaded to Google Drive under `Gideon/PDFs/` so the user can find every
Gideon-generated PDF in one place — same pattern as `create_google_doc`
which lives under `Gideon/Docs/`.

WeasyPrint requires `libpango` + `libcairo` system libraries (already
installed in the Dockerfile). It supports modern CSS — flexbox, grid,
custom fonts, `@page` rules for headers/footers, `page-break-*` for
manual layout control. Pure Python beyond the system libs.
"""

import asyncio
import io
import socket
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import structlog
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from tools.base import BaseTool

logger = structlog.get_logger()


# Hosts the PDF renderer is allowed to fetch from at render time.
# WeasyPrint resolves <img src=>, @import url(...), background-image: url(...)
# etc. against the open internet by default — that's a live SSRF / LFI surface
# whenever we render user-supplied HTML or markdown. We narrow this to the
# domains we actually need (Google Fonts) and refuse everything else. Add
# hosts here only after an explicit security review.
_ALLOWED_FETCH_HOSTS = frozenset({
    "fonts.googleapis.com",   # CSS files for the @import in print_shell.html
    "fonts.gstatic.com",      # Actual font binary files referenced by the CSS
})


def _safe_url_fetcher(url: str) -> dict[str, Any]:
    """WeasyPrint URL fetcher that refuses anything outside an allowlist.

    Defends against:
    - `file://` LFI (read /etc/passwd, secrets mounted in the pod, etc.)
    - SSRF to RFC1918 / loopback / link-local / cloud-metadata endpoints
      (e.g. 169.254.169.254 for AWS/GCP metadata, 127.0.0.1:6379 for Redis)
    - DNS rebinding via allowlisted hosts that suddenly resolve to internal IPs

    `data:` URIs are passed through unchanged since they're fully self-
    contained. Anything that isn't `data:` or an allowlisted https host
    raises — WeasyPrint catches the raise and renders without that resource.
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()

    if scheme == "data":
        from weasyprint import default_url_fetcher
        return default_url_fetcher(url)

    if scheme not in ("http", "https"):
        raise ValueError(f"PDF render blocked: unsupported URL scheme '{scheme}'")

    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_FETCH_HOSTS:
        raise ValueError(f"PDF render blocked: host '{host}' is not on the fetch allowlist")
    if scheme != "https":
        raise ValueError(f"PDF render blocked: plaintext http:// to '{host}' (use https)")

    # DNS-rebinding defense: even allowlisted hosts must not resolve to
    # internal addresses. Re-check at fetch time, accept only public IPs.
    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"PDF render blocked: could not resolve '{host}': {e}") from e
    for _fam, _type, _proto, _canon, sockaddr in addrinfo:
        if not isinstance(sockaddr, tuple):
            continue
        ip_str = sockaddr[0]
        try:
            ip = ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"PDF render blocked: '{host}' resolves to non-public IP {ip}")

    from weasyprint import default_url_fetcher
    return default_url_fetcher(url)


# Default CSS for markdown→PDF output. Aimed at a clean print look —
# generous margins, sans-serif body, monospace code blocks, subtle table
# rules. Users who want more control should generate HTML themselves and
# call `html_to_pdf` instead.
_MARKDOWN_DEFAULT_CSS = """
@page {
  size: A4;
  margin: 18mm 16mm 20mm 16mm;
  @bottom-right {
    content: counter(page) " / " counter(pages);
    font-family: -apple-system, "Segoe UI", sans-serif;
    font-size: 9pt;
    color: #888;
  }
}
html, body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  font-size: 10.5pt;
  line-height: 1.55;
  color: #1a1a1a;
}
h1 { font-size: 22pt; margin: 0 0 12pt 0; }
h2 { font-size: 16pt; margin: 18pt 0 8pt 0; border-bottom: 0.5pt solid #ddd; padding-bottom: 4pt; }
h3 { font-size: 13pt; margin: 14pt 0 6pt 0; }
h4 { font-size: 11pt; margin: 12pt 0 4pt 0; color: #444; }
p { margin: 0 0 8pt 0; }
ul, ol { margin: 0 0 8pt 0; padding-left: 18pt; }
li { margin: 0 0 3pt 0; }
code {
  font-family: "SF Mono", "Menlo", Consolas, monospace;
  font-size: 9.5pt;
  background: #f4f4f4;
  padding: 1pt 3pt;
  border-radius: 2pt;
}
pre {
  font-family: "SF Mono", "Menlo", Consolas, monospace;
  font-size: 9pt;
  background: #f7f7f7;
  border: 0.5pt solid #e0e0e0;
  border-radius: 3pt;
  padding: 8pt;
  white-space: pre-wrap;
  page-break-inside: avoid;
}
pre code { background: none; padding: 0; }
blockquote {
  margin: 8pt 0;
  padding: 0 0 0 12pt;
  border-left: 2pt solid #ccc;
  color: #555;
}
table {
  border-collapse: collapse;
  width: 100%;
  margin: 8pt 0;
  font-size: 9.5pt;
  page-break-inside: avoid;
}
th, td {
  border: 0.5pt solid #d0d0d0;
  padding: 4pt 6pt;
  text-align: left;
  vertical-align: top;
}
th { background: #f4f4f4; font-weight: 600; }
a { color: #0a66c2; text-decoration: none; }
hr { border: none; border-top: 0.5pt solid #ddd; margin: 14pt 0; }
"""


def _markdown_to_html(markdown_text: str, title: str) -> str:
    """Convert markdown to a complete HTML document with default styles.
    Uses the `markdown` library (extra extensions for tables + fenced code +
    sane-list rendering)."""
    import markdown as md  # local — heavy import, defer until tool runs
    body = md.markdown(
        markdown_text,
        extensions=["extra", "fenced_code", "tables", "sane_lists"],
    )
    safe_title = (title or "Document").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!DOCTYPE html>"
        "<html><head>"
        f"<meta charset=\"utf-8\"><title>{safe_title}</title>"
        f"<style>{_MARKDOWN_DEFAULT_CSS}</style>"
        "</head><body>"
        f"{body}"
        "</body></html>"
    )


def _render_html_to_pdf_bytes(html: str) -> bytes:
    """Render an HTML document to PDF bytes via WeasyPrint.

    Uses `_safe_url_fetcher` so the renderer can only hit allowlisted hosts
    (currently Google Fonts) — never `file://`, never internal network IPs,
    never the AWS/GCP metadata endpoint. This matters because the input HTML
    can be user-controlled (html_to_pdf) or model-generated from user
    markdown (markdown_to_pdf).
    """
    from weasyprint import HTML  # local — heavy import, defer until tool runs
    return HTML(string=html, url_fetcher=_safe_url_fetcher).write_pdf()


def _safe_pdf_filename(title: str) -> str:
    """Strip filesystem-unsafe chars and ensure a `.pdf` suffix."""
    base = "".join(c for c in (title or "document") if c.isalnum() or c in (" ", "-", "_")).strip()
    base = base or "document"
    if not base.lower().endswith(".pdf"):
        base = f"{base}.pdf"
    return base


def _upload_pdf_to_drive_sync(access_token: str, pdf_bytes: bytes, filename: str) -> dict[str, str]:
    """Upload a PDF byte buffer to Drive under `Gideon/PDFs/`. Returns the
    Drive file_id + webViewLink. Sync — call via asyncio.to_thread.

    Implementation mirrors `services.file_tracking_service.move_file_to_gideon_folder`
    for folder discovery, but uploads bytes directly via MediaIoBaseUpload
    instead of moving an existing Google-Workspace file.
    """
    creds = Credentials(token=access_token)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    # Find or create Gideon/PDFs/.
    def _find_or_create_folder(name: str, parent_id: str | None) -> str:
        query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"
        results = service.files().list(q=query, spaces="drive", fields="files(id)").execute()
        files = results.get("files", [])
        if files:
            return files[0]["id"]
        metadata: dict[str, Any] = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            metadata["parents"] = [parent_id]
        folder = service.files().create(body=metadata, fields="id").execute()
        return folder["id"]

    gideon_folder = _find_or_create_folder("Gideon", None)
    pdfs_folder = _find_or_create_folder("PDFs", gideon_folder)

    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False)
    created = service.files().create(
        body={"name": filename, "parents": [pdfs_folder]},
        media_body=media,
        fields="id, webViewLink",
    ).execute()
    return {"file_id": created["id"], "url": created.get("webViewLink", "")}


class MarkdownToPdf(BaseTool):
    @property
    def name(self) -> str:
        return "markdown_to_pdf"

    @property
    def description(self) -> str:
        return (
            "Render markdown content into a PDF and save it to Drive under Gideon/PDFs/. "
            "Supports headings, lists, fenced code blocks, tables (GFM), blockquotes, "
            "links, and inline code. The PDF is styled with sensible print defaults "
            "(A4, page numbers, sans-serif body). Use this when the user has plain "
            "markdown text. For full design control, generate HTML and call `html_to_pdf` instead. "
            "Returns the Drive file_id and a viewable URL."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "markdown": {
                    "type": "string",
                    "description": "Markdown content to render. Headings, lists, tables, code blocks all supported.",
                },
                "title": {
                    "type": "string",
                    "description": "Document title — used as the PDF filename and the HTML <title>. Don't include a `.pdf` suffix; one is added automatically.",
                },
            },
            "required": ["markdown", "title"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        title = (params.get("title") or "").strip() or "Document"
        markdown_text = params.get("markdown") or ""
        if not markdown_text.strip():
            return {"error": "Markdown content is empty."}

        html = _markdown_to_html(markdown_text, title)

        try:
            pdf_bytes = await asyncio.to_thread(_render_html_to_pdf_bytes, html)
        except Exception as e:
            return {"error": f"PDF render failed: {e}"}

        filename = _safe_pdf_filename(title)
        try:
            uploaded = await asyncio.to_thread(_upload_pdf_to_drive_sync, access_token, pdf_bytes, filename)
        except Exception as e:
            return {"error": f"Drive upload failed: {e}"}

        return {
            "file_id": uploaded["file_id"],
            "filename": filename,
            "url": uploaded["url"],
            "size_bytes": len(pdf_bytes),
            "message": f"PDF saved to Gideon/PDFs/{filename}.",
        }


class HtmlToPdf(BaseTool):
    @property
    def name(self) -> str:
        return "html_to_pdf"

    @property
    def description(self) -> str:
        return (
            "Render an HTML document into a PDF and save it to Drive under Gideon/PDFs/. "
            "Use this when the user provides HTML directly, or when full styling control "
            "is needed (page headers/footers via `@page`, custom fonts, flexbox/grid layout, "
            "page-break-* hints). For plain markdown, prefer `markdown_to_pdf` which applies "
            "sensible defaults. The HTML should be a complete document (`<!DOCTYPE html>`...) — "
            "if you pass a fragment, it'll still render but without your CSS reset. "
            "Returns the Drive file_id and a viewable URL."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "html": {
                    "type": "string",
                    "description": "Full HTML document. Inline <style> or <link> CSS is honored. Modern CSS (flexbox, grid, @page rules, custom fonts via Google Fonts URL) supported.",
                },
                "title": {
                    "type": "string",
                    "description": "Filename for the PDF (no `.pdf` suffix needed; one is added automatically).",
                },
            },
            "required": ["html", "title"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        title = (params.get("title") or "").strip() or "Document"
        html = params.get("html") or ""
        if not html.strip():
            return {"error": "HTML content is empty."}

        try:
            pdf_bytes = await asyncio.to_thread(_render_html_to_pdf_bytes, html)
        except Exception as e:
            return {"error": f"PDF render failed: {e}"}

        filename = _safe_pdf_filename(title)
        try:
            uploaded = await asyncio.to_thread(_upload_pdf_to_drive_sync, access_token, pdf_bytes, filename)
        except Exception as e:
            return {"error": f"Drive upload failed: {e}"}

        return {
            "file_id": uploaded["file_id"],
            "filename": filename,
            "url": uploaded["url"],
            "size_bytes": len(pdf_bytes),
            "message": f"PDF saved to Gideon/PDFs/{filename}.",
        }
