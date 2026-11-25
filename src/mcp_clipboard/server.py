"""Clipboard MCP Server.

MCP server that reads and writes the system clipboard — tables, text, code,
JSON, URLs, images, and more. Preserves structure when possible (e.g.
spreadsheet row/column layout) and returns non-tabular content cleanly.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import platform as _platform
import re
import sys
from pathlib import Path
from typing import Literal, get_args

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp.types import Icon

from . import __version__
from .clipboard import (
    WRITABLE_IMAGE_MIMES,
    ClipboardError,
    ClipboardSizeError,
    _detect_backend,  # private but used here for --check
    base_mime_type,
    list_clipboard_formats,
    read_clipboard,
    read_clipboard_image,
    write_clipboard,
    write_clipboard_image,
    write_clipboard_multi_format,
    write_clipboard_typed,
)
from .parser import (
    OutputFormat,
    detect_content_type,
    extract_html_text,
    format_table,
    infer_column_types,
    parse_html_table,
    parse_tsv,
)

logger = logging.getLogger(__name__)


def _is_debug() -> bool:
    """Check if debug mode is enabled via --debug flag or MCP_CLIPBOARD_DEBUG env var."""
    return "--debug" in sys.argv or os.environ.get("MCP_CLIPBOARD_DEBUG", "") == "1"


def _configure_logging() -> None:
    """Configure root logging for the mcp_clipboard package."""
    level = logging.DEBUG if _is_debug() else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("mcp_clipboard").setLevel(level)


_INSTRUCTIONS_DIR = Path(__file__).parent / "instructions"
_ICON_BASE_URL = (
    "https://raw.githubusercontent.com/cmeans/mcp-clipboard/main/src/mcp_clipboard/icons"
)


def _load_instruction(name: str) -> str:
    """Load an instruction file from the instructions/ directory."""
    path = _INSTRUCTIONS_DIR / f"{name}.md"
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        raise RuntimeError(
            f"Missing instruction file: {path}. "
            "The mcp-clipboard package may be installed incorrectly."
        ) from None


def _load_icons() -> list[Icon]:
    """Return Icon objects pointing to hosted SVGs on GitHub."""
    icons = []
    theme_map: dict[Literal["light", "dark"], str] = {
        "light": "mcp-clipboard-logo-light.svg",
        "dark": "mcp-clipboard-logo-dark.svg",
    }
    for theme, filename in theme_map.items():
        icons.append(
            Icon(
                src=f"{_ICON_BASE_URL}/{filename}",
                mime_type="image/svg+xml",
                theme=theme,
            )
        )
    return icons


mcp = MCPServer(
    "mcp_clipboard",
    instructions=_load_instruction("server"),
    icons=_load_icons(),
    version=__version__,
)


_VALID_FORMATS = set(get_args(OutputFormat))
_MAX_CONTENT_CHARS = 50_000
_MAX_WRITE_BYTES = int(os.environ.get("MCP_CLIPBOARD_MAX_WRITE_BYTES", 1_048_576))
_BINARY_MIME_PREFIXES = ("image/", "audio/", "video/")
_BINARY_MIME_EXACT = frozenset({"application/octet-stream"})

# Whitelist of image subtypes recognized as a safe `format=` value to pass
# to mcp.Image. Anything else (including parameter-laden or malformed
# clipboard-controlled MIME strings) falls back to "png" so the host
# never sees an unexpected format string.
_IMAGE_SUBTYPE_ALLOWLIST = frozenset({"png", "jpeg", "gif", "webp", "tiff", "bmp"})

# image/* entries that are text-readable (not actual binary).
_TEXT_READABLE_MIMES = frozenset({"image/svg+xml"})

# MIME type validation: type and subtype must start with a letter.
_MIME_RE = re.compile(r"^[a-zA-Z][\w.+\-]*/[a-zA-Z][\w.+\-]*(;\s*[\w.+\-]+=[\w.+\-]+)*$")

# Selections accepted by the read tools. "primary" routes to X11 PRIMARY /
# Wayland primary selection; macOS and Windows reject it explicitly. (#110)
_VALID_SELECTIONS_TOOL: frozenset[str] = frozenset({"clipboard", "primary"})


def _validate_tool_selection(selection: str) -> str | None:
    """Validate a `selection` argument from a tool call. Returns a friendly
    error message if invalid, or None if OK. The string is what the host
    model sees, so phrase it for the model."""
    if selection not in _VALID_SELECTIONS_TOOL:
        return (
            f"Invalid selection: {selection!r}. "
            f"Supported: {', '.join(sorted(_VALID_SELECTIONS_TOOL))}."
        )
    return None


def _safe_code_fence(text: str) -> str:
    """Return a backtick fence long enough to wrap ``text`` without escape.

    Markdown spec: a fenced code block can only be closed by a fence at
    least as long as the opening fence. Pick a fence one longer than any
    backtick run inside the content; minimum length 3.
    """
    longest = 0
    current = 0
    for ch in text:
        if ch == "`":
            current += 1
            if current > longest:
                longest = current
        else:
            current = 0
    return "`" * max(3, longest + 1)


async def _read_clipboard_content(
    selection: str = "clipboard",
) -> tuple[list[list[str]], str, str]:
    """Read clipboard and attempt to extract tabular data.

    Returns (rows, html, text) where rows may be empty if no table found.
    html and text are the raw clipboard content (may be empty on error).
    """
    rows: list[list[str]] = []
    html = ""
    text = ""

    # Strategy 1: Try HTML clipboard (most reliable for spreadsheets)
    try:
        html = await read_clipboard("text/html", selection)
        if html:
            rows = parse_html_table(html)
    except ClipboardError as e:
        logger.debug("HTML clipboard read failed: %s", e)

    # Strategy 2: Fall back to TSV in plain text
    if not rows:
        try:
            text = await read_clipboard("text/plain", selection)
            if text:
                rows = parse_tsv(text)
        except ClipboardError as e:
            logger.debug("Plain text clipboard read failed: %s", e)

    return rows, html, text


def _format_non_tabular(text: str) -> str:
    """Format non-tabular text content with smart detection."""
    if not text.strip():
        return "Clipboard is empty."

    # Truncate large content
    truncated = False
    if len(text) > _MAX_CONTENT_CHARS:
        text = text[:_MAX_CONTENT_CHARS]
        truncated = True

    content_type = detect_content_type(text)
    logger.debug("Non-tabular content detected as: %s", content_type)

    if content_type == "json":
        # Pretty-print JSON
        try:
            parsed = json.loads(text.strip())
            formatted = json.dumps(parsed, indent=2, ensure_ascii=False)
            fence = _safe_code_fence(formatted)
            result = f"Clipboard contains JSON:\n\n{fence}json\n{formatted}\n{fence}"
        except (json.JSONDecodeError, ValueError):
            # Defensive: detect_content_type only returns "json" after a
            # successful json.loads on the same (already-truncated) text,
            # so this branch is currently unreachable. Kept as a safety
            # net in case content_type detection ever drifts.
            result = f"Clipboard content:\n\n{text}"
    elif content_type == "url":
        result = f"Clipboard contains URL:\n\n{text.strip()}"
    elif content_type == "code":
        body = text.rstrip()
        fence = _safe_code_fence(body)
        result = f"Clipboard contains code:\n\n{fence}\n{body}\n{fence}"
    else:
        result = f"Clipboard content:\n\n{text}"

    if truncated:
        result += f"\n\n... [truncated at {_MAX_CONTENT_CHARS:,} characters]"

    return result


@mcp.tool(
    name="clipboard_paste",
    description=_load_instruction("clipboard_paste"),
    annotations={  # type: ignore[arg-type]
        "title": "Paste Clipboard",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def clipboard_paste(
    output_format: str = "markdown",
    include_schema: bool = False,
    selection: str = "clipboard",
):
    # NOTE: No return type annotation here by design.  The true type is
    # `str | Image`.  On mcp 1.x, annotating it that way made FastMCP call
    # Pydantic's `create_model()` on the union, which failed because
    # `Image` had no Pydantic schema.  mcp 2.x's MCPServer accepts the
    # annotation (no outputSchema, same results); adding it is a separate
    # cleanup.
    output_format = output_format.strip().lower()
    if output_format not in _VALID_FORMATS:
        return (
            f"Unknown output_format: {output_format!r}. "
            f"Valid options: {', '.join(sorted(_VALID_FORMATS))}"
        )
    selection = selection.strip().lower()
    selection_err = _validate_tool_selection(selection)
    if selection_err is not None:
        return selection_err

    logger.debug(
        "clipboard_paste called: output_format=%r include_schema=%r selection=%r",
        output_format,
        include_schema,
        selection,
    )

    rows, html, text = await _read_clipboard_content(selection)

    # If we found tabular data, format it
    if rows:
        row_count = len(rows)
        col_count = max(len(r) for r in rows) if rows else 0
        formatted = format_table(rows, output_format)  # type: ignore[arg-type]
        result = f"Found table: {row_count} rows \u00d7 {col_count} columns\n\n{formatted}"

        if include_schema:
            types = infer_column_types(rows)
            if types:
                headers = list(rows[0]) if rows else []
                # Pad headers if table is wider than the header row
                while len(headers) < len(types):
                    headers.append(f"Col {len(headers) + 1}")
                schema_rows = [["Column", "Type"]] + [
                    [h, t] for h, t in zip(headers, types, strict=False)
                ]
                schema_table = format_table(schema_rows, "markdown")
                result += f"\n\n**Column types:**\n\n{schema_table}"

        return result

    # Non-tabular: prefer extracted HTML text if HTML was available
    content = ""
    if html:
        content = extract_html_text(html)
    if not content:
        content = text

    # Strategy 3: RTF fallback — try text/rtf when HTML and plain text are empty
    if not content.strip():
        try:
            rtf = await read_clipboard("text/rtf", selection)
            if rtf.strip():
                truncated = len(rtf) > _MAX_CONTENT_CHARS
                display = rtf[:_MAX_CONTENT_CHARS]
                fence = _safe_code_fence(display)
                result = f"Clipboard contains rich text (RTF):\n\n{fence}\n{display}\n{fence}"
                if truncated:
                    result += f"\n\n... [truncated at {_MAX_CONTENT_CHARS:,} characters]"
                return result
        except ClipboardError as e:
            logger.debug("RTF clipboard read failed: %s", e)

    # If no text content at all, list formats and dispatch by what's there.
    if not content.strip():
        try:
            formats = await list_clipboard_formats(selection)
        except ClipboardError:
            formats = []

        # Split image/* formats: SVG is text-readable XML, everything else
        # is raster bytes. Routing SVG through the raster image path was
        # the read-side half of #129's sibling SVG round-trip bug.
        raster_formats = [
            f
            for f in formats
            if f.startswith("image/") and base_mime_type(f) not in _TEXT_READABLE_MIMES
        ]
        svg_formats = [f for f in formats if base_mime_type(f) == "image/svg+xml"]

        # Prefer raster (returns a viewable Image object the host model
        # can analyze visually) over SVG markup (returned as text below).
        if raster_formats:
            mime = next(
                (f for f in raster_formats if base_mime_type(f) == "image/png"),
                raster_formats[0],
            )
            try:
                data = await read_clipboard_image(mime, selection)
                if data:
                    fmt = base_mime_type(mime).split("/", 1)[1].lower()
                    if fmt not in _IMAGE_SUBTYPE_ALLOWLIST:
                        fmt = "png"
                    return Image(data=data, format=fmt)
            except ClipboardSizeError as e:
                return f"Clipboard image too large to return: {e}"
            except ClipboardError as e:
                logger.debug("Image read failed: %s", e)

        # SVG-only clipboards: read the markup as text and return a fenced
        # block so the host model can inspect the source.
        if svg_formats:
            try:
                svg_text = await read_clipboard(svg_formats[0], selection)
                if svg_text.strip():
                    truncated = len(svg_text) > _MAX_CONTENT_CHARS
                    display = svg_text[:_MAX_CONTENT_CHARS]
                    fence = _safe_code_fence(display)
                    result = (
                        f"Clipboard contains SVG ({svg_formats[0]}):\n\n"
                        f"{fence}svg\n{display}\n{fence}"
                    )
                    if truncated:
                        result += f"\n\n... [truncated at {_MAX_CONTENT_CHARS:,} characters]"
                    return result
            except ClipboardError as e:
                logger.debug("SVG clipboard read failed: %s", e)

        # Non-image binary (audio/video): report but cannot return.
        binary = [
            f
            for f in formats
            if (f.startswith(_BINARY_MIME_PREFIXES) or f in _BINARY_MIME_EXACT)
            and not f.startswith("image/")
        ]
        if binary:
            fmt_list = ", ".join(binary)
            return (
                f"Clipboard contains binary data ({fmt_list}) which cannot be "
                f"returned as text. Audio and video are not supported."
            )

    return _format_non_tabular(content)


@mcp.tool(
    name="clipboard_read_raw",
    description=_load_instruction("clipboard_read_raw"),
    annotations={  # type: ignore[arg-type]
        "title": "Read Raw Clipboard",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def clipboard_read_raw(
    mime_type: str = "text/plain",
    selection: str = "clipboard",
) -> str:
    base_type = base_mime_type(mime_type)
    if not _MIME_RE.match(base_type):
        return (
            f"Invalid MIME type: {mime_type!r}. "
            f"Expected format: type/subtype (e.g. text/plain, image/png)."
        )
    if (
        base_type.startswith(_BINARY_MIME_PREFIXES) or base_type in _BINARY_MIME_EXACT
    ) and base_type not in _TEXT_READABLE_MIMES:
        return (
            f"Cannot read binary MIME type '{mime_type}'. "
            f"This tool only supports text-based formats (e.g. text/plain, text/html, "
            f"image/svg+xml, application/json)."
        )
    selection = selection.strip().lower()
    selection_err = _validate_tool_selection(selection)
    if selection_err is not None:
        return selection_err

    try:
        content = await read_clipboard(mime_type, selection)
    except ClipboardError as e:
        return f"Error reading clipboard: {e}"

    if not content:
        return f"No content available for MIME type: {mime_type}"

    # Truncate very large content to avoid overwhelming the context
    if len(content) > _MAX_CONTENT_CHARS:
        return (
            f"Content ({len(content)} chars, truncated to {_MAX_CONTENT_CHARS}):\n\n"
            f"{content[:_MAX_CONTENT_CHARS]}\n\n... [truncated]"
        )

    return f"Content ({len(content)} chars):\n\n{content}"


@mcp.tool(
    name="clipboard_list_formats",
    description=_load_instruction("clipboard_list_formats"),
    annotations={  # type: ignore[arg-type]
        "title": "List Clipboard Formats",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def clipboard_list_formats(selection: str = "clipboard") -> str:
    selection = selection.strip().lower()
    selection_err = _validate_tool_selection(selection)
    if selection_err is not None:
        return selection_err
    try:
        formats = await list_clipboard_formats(selection)
    except ClipboardError as e:
        return f"Error listing clipboard formats: {e}"

    if not formats:
        return "The clipboard appears to be empty (no formats available)."

    # Highlight useful formats
    highlights: list[str] = []
    if any("html" in f.lower() for f in formats):
        highlights.append("✓ HTML available — table structure should be preserved")
    if any("text/plain" in f.lower() or f == "UTF8_STRING" for f in formats):
        highlights.append("✓ Plain text available")

    result = f"Clipboard has {len(formats)} format(s):\n\n"
    result += "\n".join(f"  • {f}" for f in formats)
    if highlights:
        result += "\n\n" + "\n".join(highlights)

    return result


@mcp.tool(
    name="clipboard_copy",
    description=_load_instruction("clipboard_copy"),
    annotations={  # type: ignore[arg-type]
        "title": "Copy to Clipboard",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def clipboard_copy(
    content: str,
    mime_type: str = "text/plain",
) -> str:
    mime_type = mime_type.strip().lower()
    if not _MIME_RE.match(mime_type):
        return (
            f"Invalid MIME type: {mime_type!r}. "
            f"Expected format: type/subtype (e.g. text/plain, text/html)."
        )
    content_bytes = len(content.encode())
    if content_bytes > _MAX_WRITE_BYTES:
        return (
            f"Content exceeds clipboard write limit "
            f"({content_bytes:,} bytes, max {_MAX_WRITE_BYTES:,}). "
            f"Set MCP_CLIPBOARD_MAX_WRITE_BYTES to increase."
        )
    is_binary = (
        any(mime_type.startswith(p) for p in _BINARY_MIME_PREFIXES)
        and mime_type not in _TEXT_READABLE_MIMES
    ) or mime_type in _BINARY_MIME_EXACT
    if is_binary:
        return (
            f"Cannot write binary MIME type {mime_type!r} to clipboard. "
            "clipboard_copy supports text content only. "
            "For PNG or JPEG images, use clipboard_copy_image."
        )
    try:
        if mime_type == "text/plain":
            await write_clipboard(content)
        else:
            await write_clipboard_typed(content, mime_type)
    except ClipboardError as e:
        return f"Error writing to clipboard: {e}"

    return f"Copied {len(content)} characters to clipboard as {mime_type}."


@mcp.tool(
    name="clipboard_copy_image",
    description=_load_instruction("clipboard_copy_image"),
    annotations={  # type: ignore[arg-type]
        "title": "Copy Image to Clipboard",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def clipboard_copy_image(
    image_data: str,
    mime_type: str = "image/png",
) -> str:
    mime_type = mime_type.strip().lower()
    if mime_type not in WRITABLE_IMAGE_MIMES:
        return (
            f"Unsupported image MIME type: {mime_type!r}. "
            f"Supported: {', '.join(sorted(WRITABLE_IMAGE_MIMES))}."
        )
    try:
        data = base64.b64decode(image_data, validate=True)
    except (binascii.Error, ValueError) as e:
        return f"image_data is not valid base64: {e}"
    try:
        await write_clipboard_image(data, mime_type)
    except ClipboardError as e:
        return f"Error writing image to clipboard: {e}"

    return f"Copied {len(data):,} bytes to clipboard as {mime_type}."


# Cap on markdown source bytes accepted by clipboard_copy_markdown. The same
# MCP_CLIPBOARD_MAX_WRITE_BYTES envelope as clipboard_copy. Larger documents
# can still be written as text/plain via clipboard_copy directly.
def _render_markdown_to_html(markdown_source: str) -> str:
    """Render a markdown string to HTML with raw HTML disabled.

    `MarkdownIt('default', {'html': False})` escapes raw HTML tags in the
    source rather than passing them through, so the rendered output is safe
    by construction (no script/iframe/etc. injection vector). A model
    wanting to write hand-crafted HTML should use clipboard_copy with
    mime_type='text/html' explicitly — that's the opt-in surface.
    """
    from markdown_it import MarkdownIt

    md = MarkdownIt("default", {"html": False})
    rendered: str = md.render(markdown_source)
    return rendered


@mcp.tool(
    name="clipboard_copy_markdown",
    description=_load_instruction("clipboard_copy_markdown"),
    annotations={  # type: ignore[arg-type]
        "title": "Copy Markdown to Clipboard (paste as rich text)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def clipboard_copy_markdown(text: str) -> str:
    content_bytes = len(text.encode("utf-8"))
    if content_bytes > _MAX_WRITE_BYTES:
        return (
            f"Markdown source exceeds clipboard write limit "
            f"({content_bytes:,} bytes, max {_MAX_WRITE_BYTES:,}). "
            f"Set MCP_CLIPBOARD_MAX_WRITE_BYTES to increase."
        )
    try:
        html = _render_markdown_to_html(text)
    except Exception as e:
        return f"Failed to render markdown: {e}"

    try:
        await write_clipboard_multi_format({"text/html": html, "text/plain": text})
    except ClipboardError as e:
        return f"Error writing to clipboard: {e}"

    return (
        f"Copied {len(text):,} characters of markdown to clipboard "
        f"(rendered to {len(html):,}-char HTML; rich-text and plain-text "
        f"both available where the platform supports atomic multi-format write)."
    )


@mcp.tool(
    name="clipboard_version",
    description=_load_instruction("clipboard_version"),
    annotations={  # type: ignore[arg-type]
        "title": "Clipboard Server Version",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def clipboard_version() -> dict[str, str]:
    return {"name": "mcp-clipboard", "version": __version__}


_HELP_TEXT = """\
mcp-clipboard: MCP server for reading and writing the system clipboard.

This is a stdio MCP server. Running it directly will start the server and
wait for JSON-RPC input on stdin; you almost certainly want to invoke it
through an MCP client (Claude Desktop, Claude Code, etc.) instead.

Usage:
  mcp-clipboard            Start the stdio MCP server.
  mcp-clipboard --check    Verify the platform backend is reachable, then exit.
  mcp-clipboard --version  Print the installed version and exit.
  mcp-clipboard --help     Print this help and exit.
  mcp-clipboard --debug    Start the server with debug logging.

Setup:
  https://github.com/cmeans/mcp-clipboard#setup
"""


def _run_check() -> int:
    """Diagnose whether the platform backend is reachable. Returns exit code."""
    print(f"mcp-clipboard {__version__}")
    print(f"Platform: {_platform.system()} ({_platform.release()})")
    try:
        backend = _detect_backend()
    except ClipboardError as e:
        print("Backend: NOT AVAILABLE", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        return 1
    print(f"Backend: {backend} (detected)")
    print("OK: mcp-clipboard should work on this system.")
    return 0


def main() -> None:
    """Entry point for the mcp-clipboard command."""
    if "--version" in sys.argv or "-V" in sys.argv:
        print(f"mcp-clipboard {__version__}")
        return
    if "--help" in sys.argv or "-h" in sys.argv:
        print(_HELP_TEXT)
        return
    if "--check" in sys.argv:
        sys.exit(_run_check())

    _configure_logging()
    # Strip --debug before MCPServer sees argv
    sys.argv = [a for a in sys.argv if a != "--debug"]
    mcp.run()


if __name__ == "__main__":
    main()
