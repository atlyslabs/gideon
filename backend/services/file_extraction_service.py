"""
File Extraction Service -- extract text content from uploaded files.

Supported formats:
- Text/code: .txt, .md, .csv, .tsv, .json, .xml, .yaml, .log, .py, .js, .ts, etc.
- PDF: via pypdfium2
- DOCX: via python-docx
- XLSX: via openpyxl
- PPTX: via python-pptx
- Images: stored as base64 for Claude vision (no text extraction)

Dependencies to install: pypdfium2, pillow, python-docx, openpyxl, python-pptx
"""

import asyncio
import base64
import io
import structlog

logger = structlog.get_logger()

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB hard limit
# Per-page pixel ceiling for PDF->image rendering — guards against a crafted or
# unusually large-dimension page making pypdfium2's renderer allocate GBs (each
# rendered pixel is ~3 bytes RGB before PNG). ~25 MP keeps a page well under
# ~75 MB in memory while staying legible for vision.
_MAX_RENDER_PIXELS = 25_000_000

# Extensions grouped by extraction method
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
    ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rb",
    ".rs", ".sql", ".sh", ".bash", ".css", ".html", ".htm", ".svg",
    ".toml", ".ini", ".cfg", ".env", ".rtf", ".c", ".cpp", ".h",
    ".swift", ".kt", ".scala", ".r", ".lua", ".pl", ".php",
}
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".doc"}
SPREADSHEET_EXTENSIONS = {".xlsx", ".xls"}
PRESENTATION_EXTENSIONS = {".pptx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}

ALL_ALLOWED_EXTENSIONS = (
    TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS | SPREADSHEET_EXTENSIONS
    | PRESENTATION_EXTENSIONS | IMAGE_EXTENSIONS
)

# MIME type to extension mapping for validation
IMAGE_MIME_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml", "image/bmp",
}


def get_extension(filename: str) -> str:
    """Get lowercase file extension including the dot."""
    import os
    _, ext = os.path.splitext(filename.lower())
    return ext


def validate_file(filename: str, size_bytes: int) -> str | None:
    """Validate file. Returns error message or None if valid."""
    if size_bytes > MAX_FILE_SIZE:
        return f"File too large ({size_bytes / 1024 / 1024:.1f} MB). Maximum is 10 MB."

    ext = get_extension(filename)
    if ext not in ALL_ALLOWED_EXTENSIONS:
        return f"Unsupported file type: {ext}. Supported: text, PDF, DOCX, XLSX, PPTX, images."

    return None


def is_image(filename: str) -> bool:
    return get_extension(filename) in IMAGE_EXTENSIONS


async def extract_text(filename: str, file_bytes: bytes) -> str:
    """Extract text content from a file. Returns empty string for images.

    The parsers (pypdfium2 / python-docx / openpyxl / python-pptx) are CPU-bound
    and synchronous, so a large document would block the event loop if run
    inline — run them in a worker thread instead. The image short-circuit is
    cheap and stays on the loop."""
    if get_extension(filename) in IMAGE_EXTENSIONS:
        return ""  # Images use vision, no text extraction
    return await asyncio.to_thread(_extract_text_sync, filename, file_bytes)


def _extract_text_sync(filename: str, file_bytes: bytes) -> str:
    """Synchronous parser dispatch — run via `asyncio.to_thread` from
    `extract_text` (and callable directly from other sync code)."""
    ext = get_extension(filename)

    if ext in TEXT_EXTENSIONS:
        return _extract_text_plain(file_bytes)

    if ext == ".pdf":
        return _extract_pdf(file_bytes)

    if ext == ".docx":
        return _extract_docx(file_bytes)

    if ext in (".xlsx", ".xls"):
        return _extract_xlsx(file_bytes)

    if ext == ".pptx":
        return _extract_pptx(file_bytes)

    if ext == ".doc":
        # .doc (legacy Word) -- try as plain text, won't be perfect
        return _extract_text_plain(file_bytes)

    return _extract_text_plain(file_bytes)


def file_to_base64(file_bytes: bytes, mime_type: str) -> str:
    """Convert image bytes to base64 data URI for Claude vision."""
    b64 = base64.b64encode(file_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{b64}"


def pdf_pages_as_png(file_bytes: bytes, max_pages: int = 5, dpi: int = 150) -> list[bytes]:
    """Render the first `max_pages` PDF pages to PNG bytes (pypdfium2).

    Provider-safe vision fallback for SCANNED PDFs (no extractable text): the
    rendered pages go to the model as IMAGE blocks, which work on OpenAI-compatible
    providers (the adapter translates them to `image_url`) AND Anthropic — unlike a
    native `document` block, which only Anthropic understands. Returns [] when
    pypdfium2 is unavailable or rendering fails (caller degrades gracefully).
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        logger.warning("pypdfium2 not installed; cannot render PDF pages to images")
        return []
    out: list[bytes] = []
    try:
        pdf = pdfium.PdfDocument(file_bytes)
        for page_num in range(min(len(pdf), max_pages)):
            page = pdf[page_num]
            zoom = dpi / 72.0
            # Clamp zoom so no page renders above _MAX_RENDER_PIXELS — a crafted
            # huge-dimension page at 150dpi could otherwise allocate GBs and kill
            # the worker despite the page-count limit.
            w_pt, h_pt = page.get_size()
            if w_pt > 0 and h_pt > 0 and (w_pt * zoom) * (h_pt * zoom) > _MAX_RENDER_PIXELS:
                zoom = (_MAX_RENDER_PIXELS / (w_pt * h_pt)) ** 0.5
            buf = io.BytesIO()
            page.render(scale=zoom).to_pil().save(buf, format="PNG")
            out.append(buf.getvalue())
        pdf.close()
    except Exception as e:
        logger.error("PDF page render failed", error=str(e))
        return []
    return out


def _extract_text_plain(file_bytes: bytes) -> str:
    """Extract as plain UTF-8 text."""
    try:
        return file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return file_bytes.decode("latin-1")
        except Exception:
            return file_bytes.decode("utf-8", errors="replace")


def _extract_pdf(file_bytes: bytes) -> str:
    """Extract text from PDF using pypdfium2."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        logger.warning("pypdfium2 not installed, falling back to raw text for PDF")
        return "[PDF extraction requires pypdfium2. Install with: pip install pypdfium2]"

    try:
        pdf = pdfium.PdfDocument(file_bytes)
        pages = []
        for page_num in range(len(pdf)):
            text = pdf[page_num].get_textpage().get_text_range()
            if text.strip():
                pages.append(f"--- Page {page_num + 1} ---\n{text}")
        pdf.close()
        return "\n\n".join(pages)
    except Exception as e:
        logger.error("PDF extraction failed", error=str(e))
        return f"[Failed to extract PDF text: {e}]"


def _extract_docx(file_bytes: bytes) -> str:
    """Extract text from DOCX using python-docx."""
    try:
        from docx import Document
    except ImportError:
        logger.warning("python-docx not installed")
        return "[DOCX extraction requires python-docx. Install with: pip install python-docx]"

    try:
        doc = Document(io.BytesIO(file_bytes))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)
    except Exception as e:
        logger.error("DOCX extraction failed", error=str(e))
        return f"[Failed to extract DOCX text: {e}]"


def _extract_xlsx(file_bytes: bytes) -> str:
    """Extract text from XLSX using openpyxl."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        logger.warning("openpyxl not installed")
        return "[XLSX extraction requires openpyxl. Install with: pip install openpyxl]"

    try:
        wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        sheets = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                if any(cells):
                    rows.append("\t".join(cells))
            if rows:
                sheets.append(f"--- Sheet: {sheet_name} ---\n" + "\n".join(rows))
        wb.close()
        return "\n\n".join(sheets)
    except Exception as e:
        logger.error("XLSX extraction failed", error=str(e))
        return f"[Failed to extract XLSX text: {e}]"


def _extract_pptx(file_bytes: bytes) -> str:
    """Extract text from PPTX using python-pptx."""
    try:
        from pptx import Presentation
    except ImportError:
        logger.warning("python-pptx not installed")
        return "[PPTX extraction requires python-pptx. Install with: pip install python-pptx]"

    try:
        prs = Presentation(io.BytesIO(file_bytes))
        slides = []
        for i, slide in enumerate(prs.slides, 1):
            texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        text = paragraph.text.strip()
                        if text:
                            texts.append(text)
            if texts:
                slides.append(f"--- Slide {i} ---\n" + "\n".join(texts))
        return "\n\n".join(slides)
    except Exception as e:
        logger.error("PPTX extraction failed", error=str(e))
        return f"[Failed to extract PPTX text: {e}]"
