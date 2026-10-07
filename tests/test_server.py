"""Tests for the MCP server tools and clipboard backend.

All clipboard access is mocked — no actual system clipboard needed.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.fastmcp.utilities.types import Image

from mcp_clipboard.clipboard import (
    ClipboardError,
    ClipboardSizeError,
    _detect_backend,
    _find_wayland_display,
    _macos_write_image,
    _macos_write_multi,
    _macos_write_typed,
    _pick_single_mime,
    _wayland_env,
    _wayland_write_image,
    _wayland_write_multi,
    _wayland_write_typed,
    _windows_html_clipboard_wrap,
    _windows_write,
    _windows_write_image,
    _windows_write_multi,
    _windows_write_typed,
    _x11_write_image,
    _x11_write_multi,
    _x11_write_typed,
    list_clipboard_formats,
    read_clipboard,
    read_clipboard_image,
    write_clipboard,
    write_clipboard_image,
    write_clipboard_multi_format,
    write_clipboard_typed,
)
from mcp_clipboard.server import (
    _load_icons,
    _load_instruction,
    clipboard_copy,
    clipboard_copy_image,
    clipboard_copy_markdown,
    clipboard_list_formats,
    clipboard_paste,
    clipboard_read_raw,
    clipboard_version,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_HTML = """
<table>
<tr><th>Name</th><th>Age</th><th>City</th></tr>
<tr><td>Alice</td><td>30</td><td>Portland</td></tr>
<tr><td>Bob</td><td>25</td><td>Seattle</td></tr>
</table>
"""

SAMPLE_TSV = "Name\tAge\tCity\nAlice\t30\tPortland\nBob\t25\tSeattle"


def _mock_read(html: str = "", text: str = ""):
    """Create a mock for read_clipboard that returns html or text by mime type."""

    async def _read(mime_type: str = "text/plain", selection: str = "clipboard") -> str:
        if mime_type == "text/html":
            return html
        return text

    return _read


def _mock_read_error(msg: str = "Command not found: wl-paste"):
    """Create a mock for read_clipboard that raises ClipboardError."""

    async def _read(mime_type: str = "text/plain", selection: str = "clipboard") -> str:
        raise ClipboardError(msg)

    return _read


# ---------------------------------------------------------------------------
# 1. clipboard_paste: tabular data (HTML)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paste_table_from_html():
    """clipboard_paste extracts and formats table data from HTML clipboard."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html=SAMPLE_HTML)):
        result = await clipboard_paste(output_format="markdown")

    assert "3 rows \u00d7 3 columns" in result
    assert "| Name" in result
    assert "| Alice" in result
    assert "| Bob" in result


@pytest.mark.asyncio
async def test_paste_table_json():
    """clipboard_paste returns valid JSON for tables when requested."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html=SAMPLE_HTML)):
        result = await clipboard_paste(output_format="json")

    assert "3 rows \u00d7 3 columns" in result
    import json

    json_part = result.split("\n\n", 1)[1]
    data = json.loads(json_part)
    assert len(data) == 2
    assert data[0]["Name"] == "Alice"


@pytest.mark.asyncio
async def test_paste_table_csv():
    """clipboard_paste returns CSV for tables when requested."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html=SAMPLE_HTML)):
        result = await clipboard_paste(output_format="csv")

    assert "3 rows \u00d7 3 columns" in result
    assert '"Name","Age","City"' in result
    assert '"Alice","30","Portland"' in result


@pytest.mark.asyncio
async def test_paste_format_case_insensitive():
    """clipboard_paste accepts output_format in any case."""
    for fmt in ("JSON", "Json", "jSoN", " json ", "CSV", "Csv", "MARKDOWN"):
        with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html=SAMPLE_HTML)):
            result = await clipboard_paste(output_format=fmt)
        assert "3 rows \u00d7 3 columns" in result, f"Failed for format {fmt!r}"


@pytest.mark.asyncio
async def test_paste_format_invalid():
    """clipboard_paste returns error message for unknown format."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html=SAMPLE_HTML)):
        result = await clipboard_paste(output_format="xml")
    assert "Unknown output_format" in result


@pytest.mark.asyncio
async def test_paste_with_schema():
    """clipboard_paste appends column-type schema when include_schema=True."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html=SAMPLE_HTML)):
        result = await clipboard_paste(output_format="markdown", include_schema=True)

    assert "3 rows \u00d7 3 columns" in result
    assert "Column types" in result
    assert "| Name" in result
    assert "| Age" in result
    # Age column should be integer
    assert "integer" in result


@pytest.mark.asyncio
async def test_paste_without_schema_default():
    """clipboard_paste does not include schema by default."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html=SAMPLE_HTML)):
        result = await clipboard_paste(output_format="markdown")

    assert "Column types" not in result


@pytest.mark.asyncio
async def test_paste_schema_not_appended_for_non_table():
    """include_schema is ignored when clipboard has no table."""
    with patch(
        "mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html="", text="hello world")
    ):
        result = await clipboard_paste(output_format="markdown", include_schema=True)

    assert "Column types" not in result
    assert "hello world" in result


def test_max_write_bytes_non_integer_raises_at_import():
    """A non-integer MCP_CLIPBOARD_MAX_WRITE_BYTES raises ValueError at module
    load (server.py runs ``int(os.environ.get(...))`` at import time).

    Run as a subprocess so the bad env var and partial-import state are
    contained -- reloading mcp_clipboard.server in-process would break
    every test that holds a reference to the module's exception classes.
    """
    import subprocess
    import sys

    env = {**os.environ, "MCP_CLIPBOARD_MAX_WRITE_BYTES": "not-a-number"}
    proc = subprocess.run(
        [sys.executable, "-c", "import mcp_clipboard.server"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "ValueError" in proc.stderr
    assert "not-a-number" in proc.stderr


def test_max_image_bytes_non_integer_raises_at_import():
    """Same for MCP_CLIPBOARD_MAX_IMAGE_BYTES on the clipboard module."""
    import subprocess
    import sys

    env = {**os.environ, "MCP_CLIPBOARD_MAX_IMAGE_BYTES": "ten-megs"}
    proc = subprocess.run(
        [sys.executable, "-c", "import mcp_clipboard.clipboard"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "ValueError" in proc.stderr
    assert "ten-megs" in proc.stderr


@pytest.mark.asyncio
async def test_paste_with_schema_pads_short_header_row():
    """When data rows are wider than the header row, the schema table
    pads with synthetic Col N labels (server.py:222-228 padding loop)."""
    # Header has 2 cells, data rows have 4. Padding should add Col 3, Col 4.
    html = (
        "<table>"
        "<tr><th>Name</th><th>Age</th></tr>"
        "<tr><td>Alice</td><td>30</td><td>Portland</td><td>Engineer</td></tr>"
        "<tr><td>Bob</td><td>25</td><td>Seattle</td><td>Designer</td></tr>"
        "</table>"
    )
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html=html)):
        result = await clipboard_paste(output_format="markdown", include_schema=True)

    assert "Column types" in result
    # Original headers preserved
    assert "| Name" in result
    assert "| Age" in result
    # Synthetic labels filled in for the wider data rows
    assert "Col 3" in result
    assert "Col 4" in result


@pytest.mark.asyncio
async def test_paste_slack_format():
    """clipboard_paste returns Slack-formatted table."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html=SAMPLE_HTML)):
        result = await clipboard_paste(output_format="slack")

    assert result.startswith("Found table:")
    assert "```" in result
    assert "|" not in result  # no pipe characters anywhere
    assert "Name" in result


@pytest.mark.asyncio
async def test_paste_jira_format():
    """clipboard_paste returns Jira wiki markup."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html=SAMPLE_HTML)):
        result = await clipboard_paste(output_format="jira")

    assert "||Name||" in result
    assert "|Alice|" in result


@pytest.mark.asyncio
async def test_paste_confluence_format():
    """clipboard_paste returns Confluence wiki markup (same as Jira)."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html=SAMPLE_HTML)):
        result_jira = await clipboard_paste(output_format="jira")
        result_confluence = await clipboard_paste(output_format="confluence")

    # Strip the "Found table: N rows \u00d7 N columns" prefix — it's identical
    assert result_jira == result_confluence


@pytest.mark.asyncio
async def test_paste_html_format():
    """clipboard_paste returns HTML table markup."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html=SAMPLE_HTML)):
        result = await clipboard_paste(output_format="html")

    assert "<table>" in result
    assert "<th>Name</th>" in result
    assert "<td>Alice</td>" in result


@pytest.mark.asyncio
async def test_paste_notion_format():
    """clipboard_paste returns GFM pipe table for Notion."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html=SAMPLE_HTML)):
        result_notion = await clipboard_paste(output_format="notion")
        result_markdown = await clipboard_paste(output_format="markdown")

    assert result_notion == result_markdown


@pytest.mark.asyncio
async def test_paste_format_invalid_unknown():
    """clipboard_paste rejects formats not in the valid set."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html=SAMPLE_HTML)):
        result = await clipboard_paste(output_format="xml")

    assert "Unknown output_format" in result
    assert "slack" in result  # error message lists valid options


# ---------------------------------------------------------------------------
# 2. clipboard_paste: TSV fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paste_tsv_fallback():
    """clipboard_paste falls back to TSV when no HTML is available."""
    with patch(
        "mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html="", text=SAMPLE_TSV)
    ):
        result = await clipboard_paste(output_format="markdown")

    assert "3 rows \u00d7 3 columns" in result
    assert "| Name" in result
    assert "| Alice" in result


@pytest.mark.asyncio
async def test_paste_tsv_when_html_errors():
    """clipboard_paste falls back to TSV when HTML read raises an error."""

    async def _mixed_read(mime_type: str = "text/plain", selection: str = "clipboard") -> str:
        if mime_type == "text/html":
            raise ClipboardError("No HTML available")
        return SAMPLE_TSV

    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mixed_read):
        result = await clipboard_paste(output_format="markdown")

    assert "3 rows \u00d7 3 columns" in result
    assert "| Alice" in result


# ---------------------------------------------------------------------------
# 3. clipboard_paste: non-tabular content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paste_plain_text():
    """clipboard_paste returns plain text when no table is found."""
    with patch(
        "mcp_clipboard.server.read_clipboard",
        side_effect=_mock_read(html="", text="Hello, this is plain text."),
    ):
        result = await clipboard_paste()

    assert "Clipboard content:" in result
    assert "Hello, this is plain text." in result


@pytest.mark.asyncio
async def test_paste_json_content():
    """clipboard_paste detects and pretty-prints JSON."""
    json_text = '{"name": "Alice", "age": 30}'
    with patch(
        "mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html="", text=json_text)
    ):
        result = await clipboard_paste()

    assert "Clipboard contains JSON:" in result
    assert "```json" in result
    assert '"name": "Alice"' in result


@pytest.mark.asyncio
async def test_paste_url():
    """clipboard_paste detects and returns URLs cleanly."""
    with patch(
        "mcp_clipboard.server.read_clipboard",
        side_effect=_mock_read(html="", text="https://example.com/path?q=1"),
    ):
        result = await clipboard_paste()

    assert "Clipboard contains URL:" in result
    assert "https://example.com/path?q=1" in result


@pytest.mark.asyncio
async def test_paste_code_snippet():
    """clipboard_paste detects code and wraps it in a code block."""
    code = "def hello():\n    print('hello world')\n\nhello()"
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html="", text=code)):
        result = await clipboard_paste()

    assert "Clipboard contains code:" in result
    assert "```" in result
    assert "def hello():" in result


@pytest.mark.asyncio
async def test_paste_code_with_triple_backticks_uses_longer_fence():
    """Code containing ``` must not break out of its enclosing markdown fence."""
    # Strong code pattern (def + parens) so detect_content_type returns "code".
    code = "def f():\n    return '```evil```'"
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html="", text=code)):
        result = await clipboard_paste()

    assert "```evil```" in result  # content preserved verbatim
    # Opening fence must be at least 4 backticks (longer than the embedded run).
    assert "````\n" in result


@pytest.mark.asyncio
async def test_paste_json_with_triple_backticks_in_string_uses_longer_fence():
    """JSON whose string values contain ``` must not break out of the json fence."""
    json_text = '{"snippet": "look: ```bad```"}'
    with patch(
        "mcp_clipboard.server.read_clipboard",
        side_effect=_mock_read(html="", text=json_text),
    ):
        result = await clipboard_paste()

    assert "Clipboard contains JSON:" in result
    assert "```bad```" in result
    assert "````json\n" in result


@pytest.mark.asyncio
async def test_paste_html_without_table():
    """clipboard_paste extracts text from HTML that has no table."""
    html = "<p>This is a <b>rich text</b> paragraph.</p><p>Second paragraph.</p>"
    with patch(
        "mcp_clipboard.server.read_clipboard",
        side_effect=_mock_read(html=html, text="This is a rich text paragraph. Second paragraph."),
    ):
        result = await clipboard_paste()

    assert "rich text" in result
    assert "Second paragraph" in result


@pytest.mark.asyncio
async def test_paste_empty_clipboard():
    """clipboard_paste returns empty message when clipboard is empty."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html="", text="")):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=[]):
            result = await clipboard_paste()

    assert "Clipboard is empty" in result


@pytest.mark.asyncio
async def test_paste_both_fail():
    """clipboard_paste returns empty message when both reads fail."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read_error()):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=[]):
            result = await clipboard_paste()

    assert "Clipboard is empty" in result


@pytest.mark.asyncio
async def test_paste_large_content_truncated():
    """clipboard_paste truncates content over 50KB and enforces the size bound."""
    huge = "x" * 100_000
    with patch("mcp_clipboard.server.read_clipboard", side_effect=_mock_read(html="", text=huge)):
        result = await clipboard_paste()

    assert "truncated" in result.lower()
    # The truncation message adds a small suffix; bound must stay well below input size.
    assert len(result) < 60_000, f"expected bounded output, got {len(result):,} chars"


# ---------------------------------------------------------------------------
# 4. clipboard_list_formats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_formats_with_html():
    """clipboard_list_formats highlights HTML availability."""
    mock_formats = ["text/html", "text/plain", "UTF8_STRING", "TARGETS"]

    with patch("mcp_clipboard.server.list_clipboard_formats", return_value=mock_formats):
        result = await clipboard_list_formats()

    assert "4 format(s)" in result
    assert "text/html" in result
    assert "✓ HTML available" in result
    assert "✓ Plain text available" in result


@pytest.mark.asyncio
async def test_list_formats_text_only():
    """clipboard_list_formats works when only plain text is available."""
    mock_formats = ["text/plain", "UTF8_STRING"]

    with patch("mcp_clipboard.server.list_clipboard_formats", return_value=mock_formats):
        result = await clipboard_list_formats()

    assert "2 format(s)" in result
    assert "✓ Plain text available" in result
    # Should NOT have the HTML highlight
    assert "HTML available" not in result


@pytest.mark.asyncio
async def test_list_formats_empty():
    """clipboard_list_formats handles empty clipboard."""
    with patch("mcp_clipboard.server.list_clipboard_formats", return_value=[]):
        result = await clipboard_list_formats()

    assert "empty" in result.lower()


@pytest.mark.asyncio
async def test_list_formats_error():
    """clipboard_list_formats handles ClipboardError gracefully."""
    with patch(
        "mcp_clipboard.server.list_clipboard_formats",
        side_effect=ClipboardError("No clipboard tool"),
    ):
        result = await clipboard_list_formats()

    assert "Error" in result
    assert "No clipboard tool" in result


# ---------------------------------------------------------------------------
# 5. clipboard backend error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_clipboard_command_not_found():
    """read_clipboard raises ClipboardError when the clipboard tool isn't installed."""
    with patch("mcp_clipboard.clipboard._get_backend", return_value="wayland"):
        with patch(
            "mcp_clipboard.clipboard._run",
            side_effect=ClipboardError("Command not found: wl-paste"),
        ):
            with pytest.raises(ClipboardError, match="Command not found"):
                await read_clipboard("text/html")


@pytest.mark.asyncio
async def test_read_clipboard_timeout():
    """read_clipboard raises ClipboardError on timeout."""
    with patch("mcp_clipboard.clipboard._get_backend", return_value="wayland"):
        with patch(
            "mcp_clipboard.clipboard._run",
            side_effect=ClipboardError("Clipboard command timed out"),
        ):
            with pytest.raises(ClipboardError, match="timed out"):
                await read_clipboard("text/html")


@pytest.mark.asyncio
async def test_read_raw_returns_content():
    """clipboard_read_raw returns the raw content for a given MIME type."""
    with patch(
        "mcp_clipboard.server.read_clipboard", return_value="<table><tr><td>hi</td></tr></table>"
    ):
        result = await clipboard_read_raw(mime_type="text/html")

    assert "<table>" in result
    assert "chars" in result


@pytest.mark.asyncio
async def test_read_raw_truncates_large_content():
    """clipboard_read_raw truncates very large clipboard content."""
    huge = "x" * 100_000
    with patch("mcp_clipboard.server.read_clipboard", return_value=huge):
        result = await clipboard_read_raw(mime_type="text/plain")

    assert "truncated" in result.lower()
    # Should not return the full 100k
    assert len(result) < 60_000


@pytest.mark.asyncio
async def test_read_raw_error():
    """clipboard_read_raw handles ClipboardError gracefully."""
    with patch("mcp_clipboard.server.read_clipboard", side_effect=ClipboardError("fail")):
        result = await clipboard_read_raw(mime_type="text/html")

    assert "Error" in result
    assert "fail" in result


@pytest.mark.asyncio
async def test_read_raw_empty():
    """clipboard_read_raw handles empty content for a MIME type."""
    with patch("mcp_clipboard.server.read_clipboard", return_value=""):
        result = await clipboard_read_raw(mime_type="text/html")

    assert "No content available" in result


@pytest.mark.asyncio
async def test_read_raw_rejects_binary_mime():
    """clipboard_read_raw refuses to read binary MIME types like image/png."""
    result = await clipboard_read_raw(mime_type="image/png")

    assert "Cannot read binary" in result
    assert "image/png" in result
    assert "text-based formats" in result


@pytest.mark.asyncio
async def test_read_raw_rejects_audio_mime():
    """clipboard_read_raw refuses audio MIME types."""
    result = await clipboard_read_raw(mime_type="audio/wav")

    assert "Cannot read binary" in result


@pytest.mark.asyncio
async def test_read_raw_rejects_video_mime():
    """clipboard_read_raw refuses video MIME types."""
    result = await clipboard_read_raw(mime_type="video/mp4")

    assert "Cannot read binary" in result


@pytest.mark.asyncio
async def test_read_raw_allows_svg():
    """clipboard_read_raw allows image/svg+xml as text-readable."""
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="50"/></svg>'
    with patch("mcp_clipboard.server.read_clipboard", new_callable=AsyncMock, return_value=svg):
        result = await clipboard_read_raw(mime_type="image/svg+xml")

    assert "circle" in result
    assert "Cannot read binary" not in result


@pytest.mark.asyncio
async def test_read_raw_allows_application_json():
    """clipboard_read_raw allows application/json as text-readable."""
    json_str = '{"key": "value"}'
    with patch(
        "mcp_clipboard.server.read_clipboard", new_callable=AsyncMock, return_value=json_str
    ):
        result = await clipboard_read_raw(mime_type="application/json")

    assert "key" in result
    assert "Cannot read binary" not in result


# ---------------------------------------------------------------------------
# 6. clipboard_paste with binary clipboard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paste_returns_image():
    """clipboard_paste returns Image when clipboard has image data."""
    fake_png = b"\x89PNG\r\n\x1a\nfakedata"
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch(
            "mcp_clipboard.server.list_clipboard_formats", return_value=["image/png", "image/tiff"]
        ):
            with patch("mcp_clipboard.server.read_clipboard_image", return_value=fake_png):
                result = await clipboard_paste()

    assert isinstance(result, Image)
    assert result.data == fake_png


@pytest.mark.asyncio
async def test_paste_empty_clipboard_no_binary():
    """clipboard_paste returns 'empty' when clipboard has no text and no binary."""
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=[]):
            result = await clipboard_paste()

    assert "empty" in result.lower()


# ---------------------------------------------------------------------------
# 7. Wayland auto-detection
# ---------------------------------------------------------------------------


def _fake_runtime_dir(tmp_path, sockets=("wayland-0",)):
    """Create fake Wayland socket files in a temp dir and return the path.

    Wayland is Linux-only; AF_UNIX is unavailable on Windows and macOS's
    pytest tmp_path under /private/var/folders/... overruns the AF_UNIX
    104-byte path limit. Skip the calling test cleanly on non-Linux rather
    than producing a false failure for Linux-specific code paths."""
    import socket
    import sys as _sys

    if _sys.platform != "linux":
        import pytest as _pytest

        _pytest.skip("Wayland AF_UNIX socket helpers are Linux-only")

    for name in sockets:
        sock_path = tmp_path / name
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(sock_path))
        s.close()
        # Also create corresponding .lock file (should be ignored)
        (tmp_path / f"{name}.lock").touch()

    return str(tmp_path)


def test_find_wayland_display_discovers_socket(tmp_path):
    """_find_wayland_display finds wayland-0 in XDG_RUNTIME_DIR."""
    runtime = _fake_runtime_dir(tmp_path)
    with patch.dict("os.environ", {"XDG_RUNTIME_DIR": runtime}, clear=False):
        result = _find_wayland_display()
    assert result == "wayland-0"


def test_find_wayland_display_picks_lowest(tmp_path):
    """_find_wayland_display picks the lowest-numbered socket."""
    runtime = _fake_runtime_dir(tmp_path, sockets=("wayland-1", "wayland-0", "wayland-2"))
    with patch.dict("os.environ", {"XDG_RUNTIME_DIR": runtime}, clear=False):
        result = _find_wayland_display()
    assert result == "wayland-0"


def test_find_wayland_display_no_sockets(tmp_path):
    """_find_wayland_display returns None when no sockets exist."""
    with patch.dict("os.environ", {"XDG_RUNTIME_DIR": str(tmp_path)}, clear=False):
        result = _find_wayland_display()
    assert result is None


def test_find_wayland_display_no_runtime_dir():
    """_find_wayland_display returns None when XDG_RUNTIME_DIR doesn't exist."""
    with patch.dict("os.environ", {"XDG_RUNTIME_DIR": "/nonexistent"}, clear=False):
        result = _find_wayland_display()
    assert result is None


def test_find_wayland_display_ignores_lock_files(tmp_path):
    """_find_wayland_display ignores .lock files."""
    (tmp_path / "wayland-0.lock").touch()
    with patch.dict("os.environ", {"XDG_RUNTIME_DIR": str(tmp_path)}, clear=False):
        result = _find_wayland_display()
    assert result is None


def test_find_wayland_display_returns_none_on_iterdir_oserror(tmp_path):
    """If iterdir() raises OSError (e.g. permission denied on the runtime
    directory) _find_wayland_display swallows the error and returns None
    rather than propagating an exception out to the dispatch layer.
    Covers the bare-except OSError handler that runs when the directory
    is technically a directory but is unreadable."""
    import sys as _sys
    from pathlib import Path as _Path

    if _sys.platform != "linux":
        pytest.skip("Wayland runtime-dir tests are Linux-only")

    with patch.dict("os.environ", {"XDG_RUNTIME_DIR": str(tmp_path)}, clear=False):
        with patch.object(_Path, "iterdir", side_effect=OSError("Permission denied")):
            result = _find_wayland_display()
    assert result is None


def test_wayland_env_returns_none_when_runtime_dir_is_missing():
    """_wayland_env's `if not Path(xdg_runtime).is_dir(): return None` --
    the path computed from $XDG_RUNTIME_DIR (or /run/user/<uid> fallback)
    does not exist on disk, so wl-paste cannot connect and we return None
    rather than handing back a broken env dict."""
    with patch.dict(
        "os.environ",
        {"XDG_RUNTIME_DIR": "/nonexistent-xdg-runtime-dir"},
        clear=False,
    ):
        # WAYLAND_DISPLAY explicitly absent so we don't take the early
        # both-set return at the top of _wayland_env.
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("WAYLAND_DISPLAY", None)
            result = _wayland_env()
    assert result is None


def test_wayland_env_returns_none_when_both_set():
    """_wayland_env returns None when both WAYLAND_DISPLAY and XDG_RUNTIME_DIR are set."""
    with patch.dict(
        "os.environ",
        {
            "WAYLAND_DISPLAY": "wayland-0",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        },
        clear=False,
    ):
        assert _wayland_env() is None


def test_wayland_env_injects_display(tmp_path):
    """_wayland_env injects WAYLAND_DISPLAY when not set but socket exists."""
    runtime = _fake_runtime_dir(tmp_path)
    env_patch = {"XDG_RUNTIME_DIR": runtime}
    with patch.dict("os.environ", env_patch, clear=False):
        import os

        os.environ.pop("WAYLAND_DISPLAY", None)
        env = _wayland_env()

    assert env is not None
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env["XDG_RUNTIME_DIR"] == runtime


def test_wayland_env_injects_runtime_dir(tmp_path):
    """_wayland_env injects XDG_RUNTIME_DIR when not set."""
    _fake_runtime_dir(tmp_path)
    with patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}, clear=False):
        import os

        os.environ.pop("XDG_RUNTIME_DIR", None)
        with patch("mcp_clipboard.clipboard.os.getuid", return_value=1000):
            # Point the fallback path at our tmp_path with real sockets
            with patch("mcp_clipboard.clipboard.Path") as mock_path:
                mock_path.return_value.is_dir.return_value = True
                # Use the real tmp_path for the env value
                env = _wayland_env()

    assert env is not None
    assert "XDG_RUNTIME_DIR" in env


def test_wayland_env_injects_both_when_neither_set(tmp_path):
    """_wayland_env injects both vars when neither is set (Claude Desktop scenario)."""
    _fake_runtime_dir(tmp_path)
    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ.pop("XDG_RUNTIME_DIR", None)
        with patch("mcp_clipboard.clipboard.os.getuid", return_value=1000):
            with patch("mcp_clipboard.clipboard.Path") as mock_path:
                # Make Path(xdg_runtime).is_dir() return True
                mock_path.return_value.is_dir.return_value = True
                # But use real _find_wayland_display with the actual tmp_path
                with patch(
                    "mcp_clipboard.clipboard._find_wayland_display", return_value="wayland-0"
                ):
                    env = _wayland_env()

    assert env is not None
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert env["WAYLAND_DISPLAY"] == "wayland-0"


def test_wayland_env_no_socket_still_passes_runtime_dir(tmp_path):
    """_wayland_env still provides XDG_RUNTIME_DIR even when no socket is found."""
    env_patch = {"XDG_RUNTIME_DIR": str(tmp_path)}
    with patch.dict("os.environ", env_patch, clear=False):
        import os

        os.environ.pop("WAYLAND_DISPLAY", None)
        env = _wayland_env()

    # env returned so XDG_RUNTIME_DIR is inherited; no WAYLAND_DISPLAY added
    assert env is not None
    assert "WAYLAND_DISPLAY" not in env


def test_detect_backend_wayland_via_socket(tmp_path):
    """_detect_backend selects wayland when socket exists but env vars are unset."""
    runtime = _fake_runtime_dir(tmp_path)
    env_patch = {"XDG_RUNTIME_DIR": runtime}
    with patch.dict("os.environ", env_patch, clear=False):
        import os

        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ.pop("XDG_SESSION_TYPE", None)
        with patch("mcp_clipboard.clipboard.platform.system", return_value="Linux"):
            with patch("mcp_clipboard.clipboard.shutil.which", return_value="/usr/bin/wl-paste"):
                result = _detect_backend()

    assert result == "wayland"


def test_detect_backend_prefers_env_var_over_socket():
    """_detect_backend uses WAYLAND_DISPLAY env var without needing socket scan."""
    with patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}, clear=False):
        with patch("mcp_clipboard.clipboard.platform.system", return_value="Linux"):
            with patch("mcp_clipboard.clipboard.shutil.which", return_value="/usr/bin/wl-paste"):
                result = _detect_backend()

    assert result == "wayland"


# ---------------------------------------------------------------------------
# 8. macOS backend
# ---------------------------------------------------------------------------

from mcp_clipboard.clipboard import (
    _macos_list_formats,
    _macos_read,
    _windows_list_formats,
    _windows_read,
    _x11_list_formats,
    _x11_read,
)


@pytest.mark.asyncio
async def test_macos_read_html():
    """_macos_read uses osascript for text/html."""
    with patch(
        "mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value="<b>hi</b>"
    ) as mock_run:
        result = await _macos_read("text/html")

    assert result == "<b>hi</b>"
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "osascript"


@pytest.mark.asyncio
async def test_macos_read_plain():
    """_macos_read uses pbpaste for text/plain."""
    with patch(
        "mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value="hello"
    ) as mock_run:
        result = await _macos_read("text/plain")

    assert result == "hello"
    cmd = mock_run.call_args[0][0]
    assert cmd == ["pbpaste"]


@pytest.mark.asyncio
async def test_macos_read_unsupported_returns_empty():
    """_macos_read returns empty string for unsupported MIME types."""
    result = await _macos_read("text/xml")
    assert result == ""


@pytest.mark.asyncio
async def test_macos_read_rtf():
    """_macos_read uses osascript for text/rtf."""
    rtf_content = r"{\rtf1\ansi Hello, {\b world}!}"
    with patch(
        "mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value=rtf_content
    ) as mock_run:
        result = await _macos_read("text/rtf")

    assert result == rtf_content
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "osascript"
    assert "public.rtf" in mock_run.call_args[0][0][-1]


@pytest.mark.asyncio
async def test_macos_list_formats_maps_uti_to_mime():
    """_macos_list_formats maps known UTIs to MIME types."""
    raw_output = "public.html\npublic.utf8-plain-text\npublic.png\n"
    with patch("mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value=raw_output):
        result = await _macos_list_formats()

    assert result == ["text/html", "text/plain", "image/png"]


@pytest.mark.asyncio
async def test_macos_list_formats_passthrough_unknown():
    """_macos_list_formats passes through unknown UTIs as-is."""
    raw_output = "public.html\ncom.apple.something-custom\n"
    with patch("mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value=raw_output):
        result = await _macos_list_formats()

    assert result == ["text/html", "com.apple.something-custom"]


@pytest.mark.asyncio
async def test_macos_list_formats_deduplicates_mime_types():
    """_macos_list_formats deduplicates when multiple UTIs map to the same MIME type."""
    raw_output = "public.html\npublic.utf8-plain-text\npublic.plain-text\npublic.png\n"
    with patch("mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value=raw_output):
        result = await _macos_list_formats()

    assert result == ["text/html", "text/plain", "image/png"]


# ---------------------------------------------------------------------------
# 9. Windows backend
# ---------------------------------------------------------------------------


# Windows backend uses the pywin32 win32clipboard wrapper directly via
# clipboard_win32.py. These tests mock the wrapper module so they pass on
# Linux CI; integration tests that exercise real pywin32 against a live
# Windows clipboard are gated by @pytest.mark.integration.


@pytest.mark.parametrize(
    "mime_type",
    ["text/plain", "text/html", "text/rtf", "image/svg+xml"],
)
@pytest.mark.asyncio
async def test_windows_read_dispatches_to_win32_wrapper(mime_type):
    """_windows_read forwards each supported MIME to clipboard_win32.read_text."""
    with patch("mcp_clipboard.clipboard_win32.read_text", return_value="payload") as mock_read:
        result = await _windows_read(mime_type)

    assert result == "payload"
    mock_read.assert_called_once_with(mime_type)


@pytest.mark.asyncio
async def test_windows_read_unsupported_returns_empty():
    """_windows_read returns empty string for unsupported MIME types
    without ever calling the win32 wrapper."""
    with patch("mcp_clipboard.clipboard_win32.read_text") as mock_read:
        result = await _windows_read("text/xml")

    assert result == ""
    mock_read.assert_not_called()


@pytest.mark.asyncio
async def test_windows_read_wraps_win32_errors_as_clipboard_error():
    """Errors from the win32 wrapper surface as ClipboardError so the rest
    of the dispatch layer (_read_clipboard_content) can handle them
    uniformly with the other backends."""
    with patch(
        "mcp_clipboard.clipboard_win32.read_text",
        side_effect=RuntimeError("OpenClipboard failed"),
    ):
        with pytest.raises(ClipboardError, match="Windows clipboard read failed"):
            await _windows_read("text/plain")


@pytest.mark.asyncio
async def test_windows_list_formats_maps_names_to_mime():
    """_windows_list_formats maps known Windows native names to MIME types and
    deduplicates collisions (Text and UnicodeText both map to text/plain)."""
    with patch(
        "mcp_clipboard.clipboard_win32.list_formats",
        return_value=["HTML Format", "Text", "UnicodeText", "PNG"],
    ):
        result = await _windows_list_formats()

    assert result == ["text/html", "text/plain", "image/png"]


@pytest.mark.asyncio
async def test_windows_list_formats_passthrough_unknown():
    """_windows_list_formats passes through unknown format names as-is."""
    with patch(
        "mcp_clipboard.clipboard_win32.list_formats",
        return_value=["HTML Format", "System.String"],
    ):
        result = await _windows_list_formats()

    assert result == ["text/html", "System.String"]


@pytest.mark.asyncio
async def test_windows_list_formats_deduplicates_mime_types():
    """Multiple native names mapping to the same MIME collapse to one entry."""
    with patch(
        "mcp_clipboard.clipboard_win32.list_formats",
        return_value=["Text", "UnicodeText", "Text", "HTML Format"],
    ):
        result = await _windows_list_formats()

    assert result == ["text/plain", "text/html"]


@pytest.mark.asyncio
async def test_windows_list_formats_wraps_win32_errors_as_clipboard_error():
    """Errors from the win32 wrapper surface as ClipboardError."""
    with patch(
        "mcp_clipboard.clipboard_win32.list_formats",
        side_effect=RuntimeError("OpenClipboard failed"),
    ):
        with pytest.raises(ClipboardError, match="Windows clipboard list failed"):
            await _windows_list_formats()


# ---------------------------------------------------------------------------
# 10. X11 backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_x11_read_html():
    """_x11_read calls xclip with correct target for text/html."""
    with patch(
        "mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value="<b>hi</b>"
    ) as mock_run:
        result = await _x11_read("text/html")

    assert result == "<b>hi</b>"
    cmd = mock_run.call_args[0][0]
    assert cmd == ["xclip", "-selection", "clipboard", "-target", "text/html", "-o"]


@pytest.mark.asyncio
async def test_x11_read_plain():
    """_x11_read calls xclip with correct target for text/plain."""
    with patch(
        "mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value="hello"
    ) as mock_run:
        result = await _x11_read("text/plain")

    assert result == "hello"
    cmd = mock_run.call_args[0][0]
    assert cmd == ["xclip", "-selection", "clipboard", "-target", "text/plain", "-o"]


@pytest.mark.asyncio
async def test_x11_list_formats():
    """_x11_list_formats calls xclip with TARGETS and parses output."""
    raw_output = "text/html\ntext/plain\nimage/png\n"
    with patch(
        "mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value=raw_output
    ) as mock_run:
        result = await _x11_list_formats()

    assert result == ["text/html", "text/plain", "image/png"]
    cmd = mock_run.call_args[0][0]
    assert cmd == ["xclip", "-selection", "clipboard", "-target", "TARGETS", "-o"]


@pytest.mark.asyncio
async def test_x11_list_formats_strips_whitespace():
    """_x11_list_formats strips whitespace and skips blank lines."""
    raw_output = "  text/html  \n\n  text/plain  \n"
    with patch("mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value=raw_output):
        result = await _x11_list_formats()

    assert result == ["text/html", "text/plain"]


# ---------------------------------------------------------------------------
# 11. _load_instruction()
# ---------------------------------------------------------------------------


def test_load_instruction_returns_content():
    """_load_instruction loads and strips instruction file content."""
    result = _load_instruction("server")
    assert isinstance(result, str)
    assert len(result) > 0
    # Should not have leading/trailing whitespace
    assert result == result.strip()


def test_load_instruction_missing_file():
    """_load_instruction raises RuntimeError for missing files."""
    with pytest.raises(RuntimeError, match="Missing instruction file"):
        _load_instruction("nonexistent_file")


# ---------------------------------------------------------------------------
# 11b. _load_icons()
# ---------------------------------------------------------------------------


def test_load_icons_returns_icons():
    """_load_icons returns Icon objects with GitHub URLs for light and dark themes."""
    icons = _load_icons()
    assert len(icons) == 2
    themes = {icon.theme for icon in icons}
    assert themes == {"light", "dark"}
    for icon in icons:
        assert icon.src.startswith("https://raw.githubusercontent.com/")
        assert icon.src.endswith(".svg")
        assert icon.mimeType == "image/svg+xml"


# ---------------------------------------------------------------------------
# 12. _detect_backend() platform coverage
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_backend_cache():
    """Ensure the module-global ``_backend`` cache is None around every test.

    Isolates tests from each other: any test that mutates ``cb._backend``
    (directly or via ``_get_backend``) must not leak state to the next test.
    """
    from mcp_clipboard.clipboard import reset_backend_cache

    reset_backend_cache()
    try:
        yield
    finally:
        reset_backend_cache()


def test_detect_backend_darwin():
    """_detect_backend returns 'macos' on Darwin."""
    with patch("mcp_clipboard.clipboard.platform.system", return_value="Darwin"):
        result = _detect_backend()

    assert result == "macos"


def test_detect_backend_windows():
    """_detect_backend returns 'windows' on Windows."""
    with patch("mcp_clipboard.clipboard.platform.system", return_value="Windows"):
        result = _detect_backend()

    assert result == "windows"


def test_detect_backend_unsupported():
    """_detect_backend raises ClipboardError on unsupported platforms."""
    with patch("mcp_clipboard.clipboard.platform.system", return_value="FreeBSD"):
        with pytest.raises(ClipboardError, match="Unsupported platform: FreeBSD"):
            _detect_backend()


def test_detect_backend_linux_no_tools():
    """_detect_backend raises ClipboardError when no clipboard tools are installed."""
    with patch("mcp_clipboard.clipboard.platform.system", return_value="Linux"):
        with patch("mcp_clipboard.clipboard.shutil.which", return_value=None):
            with patch.dict("os.environ", {}, clear=True):
                with pytest.raises(ClipboardError, match="No clipboard tool found"):
                    _detect_backend()


def test_get_backend_env_override():
    """MCP_CLIPBOARD_BACKEND env var overrides auto-detection."""
    import mcp_clipboard.clipboard as cb

    with patch.dict("os.environ", {"MCP_CLIPBOARD_BACKEND": "x11"}):
        result = cb._get_backend()
    assert result == "x11"


def test_get_backend_env_override_invalid():
    """MCP_CLIPBOARD_BACKEND with invalid value raises ClipboardError."""
    import mcp_clipboard.clipboard as cb

    with patch.dict("os.environ", {"MCP_CLIPBOARD_BACKEND": "invalid"}):
        with pytest.raises(ClipboardError, match="Invalid MCP_CLIPBOARD_BACKEND"):
            cb._get_backend()


def test_get_backend_auto_detect_when_no_override():
    """Without MCP_CLIPBOARD_BACKEND, _get_backend uses auto-detection."""
    import mcp_clipboard.clipboard as cb

    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("MCP_CLIPBOARD_BACKEND", None)
        with patch("mcp_clipboard.clipboard._detect_backend", return_value="wayland"):
            result = cb._get_backend()
    assert result == "wayland"


# ---------------------------------------------------------------------------
# 13. Backend dispatch (read_clipboard / list_clipboard_formats)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_clipboard_dispatches_to_wayland():
    """read_clipboard calls the wayland reader when backend is wayland."""
    mock_reader = AsyncMock(return_value="hello")
    with patch("mcp_clipboard.clipboard._get_backend", return_value="wayland"):
        with patch.dict("mcp_clipboard.clipboard._READERS", {"wayland": mock_reader}):
            result = await read_clipboard("text/plain")

    assert result == "hello"
    mock_reader.assert_called_once_with("text/plain", "clipboard")


@pytest.mark.asyncio
async def test_read_clipboard_dispatches_to_x11():
    """read_clipboard calls the x11 reader when backend is x11."""
    mock_reader = AsyncMock(return_value="hello")
    with patch("mcp_clipboard.clipboard._get_backend", return_value="x11"):
        with patch.dict("mcp_clipboard.clipboard._READERS", {"x11": mock_reader}):
            result = await read_clipboard("text/plain")

    assert result == "hello"
    mock_reader.assert_called_once_with("text/plain", "clipboard")


@pytest.mark.asyncio
async def test_read_clipboard_dispatches_to_macos():
    """read_clipboard calls the macos reader when backend is macos."""
    mock_reader = AsyncMock(return_value="hello")
    with patch("mcp_clipboard.clipboard._get_backend", return_value="macos"):
        with patch.dict("mcp_clipboard.clipboard._READERS", {"macos": mock_reader}):
            result = await read_clipboard("text/plain")

    assert result == "hello"
    mock_reader.assert_called_once_with("text/plain", "clipboard")


@pytest.mark.asyncio
async def test_read_clipboard_dispatches_to_windows():
    """read_clipboard calls the windows reader when backend is windows."""
    mock_reader = AsyncMock(return_value="hello")
    with patch("mcp_clipboard.clipboard._get_backend", return_value="windows"):
        with patch.dict("mcp_clipboard.clipboard._READERS", {"windows": mock_reader}):
            result = await read_clipboard("text/plain")

    assert result == "hello"
    mock_reader.assert_called_once_with("text/plain", "clipboard")


@pytest.mark.asyncio
async def test_list_clipboard_formats_dispatches_to_backend():
    """list_clipboard_formats calls the correct backend lister."""
    mock_lister = AsyncMock(return_value=["text/plain"])
    with patch("mcp_clipboard.clipboard._get_backend", return_value="x11"):
        with patch.dict("mcp_clipboard.clipboard._FORMAT_LISTERS", {"x11": mock_lister}):
            result = await list_clipboard_formats()

    assert result == ["text/plain"]
    mock_lister.assert_called_once()


# ---------------------------------------------------------------------------
# 14. Image passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paste_image_prefers_png():
    """clipboard_paste prefers image/png when multiple image formats available."""
    fake_png = b"\x89PNG\r\n\x1a\n"
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch(
            "mcp_clipboard.server.list_clipboard_formats", return_value=["image/tiff", "image/png"]
        ):
            with patch(
                "mcp_clipboard.server.read_clipboard_image", return_value=fake_png
            ) as mock_img:
                result = await clipboard_paste()

    mock_img.assert_called_once_with("image/png", "clipboard")
    assert isinstance(result, Image)


@pytest.mark.asyncio
async def test_paste_image_falls_back_to_first():
    """clipboard_paste uses first image format when PNG not available."""
    fake_tiff = b"TIFF_DATA"
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch(
            "mcp_clipboard.server.list_clipboard_formats", return_value=["image/tiff", "image/jpeg"]
        ):
            with patch(
                "mcp_clipboard.server.read_clipboard_image", return_value=fake_tiff
            ) as mock_img:
                result = await clipboard_paste()

    mock_img.assert_called_once_with("image/tiff", "clipboard")
    assert isinstance(result, Image)


@pytest.mark.asyncio
async def test_paste_audio_still_reports_text():
    """clipboard_paste returns text message for audio/video (not image)."""
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=["audio/mpeg"]):
            result = await clipboard_paste()

    assert isinstance(result, str)
    assert "binary data" in result
    assert "audio/mpeg" in result


@pytest.mark.asyncio
async def test_paste_image_read_failure_graceful():
    """clipboard_paste handles image read failure gracefully."""
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=["image/png"]):
            with patch(
                "mcp_clipboard.server.read_clipboard_image",
                side_effect=ClipboardError("read failed"),
            ):
                result = await clipboard_paste()

    # Should fall through to empty clipboard message
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_paste_image_empty_data():
    """clipboard_paste handles empty image data (format listed but no data)."""
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=["image/png"]):
            with patch("mcp_clipboard.server.read_clipboard_image", return_value=b""):
                result = await clipboard_paste()

    # Empty data should fall through
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_read_clipboard_image_size_cap():
    """read_clipboard_image raises ClipboardSizeError when bytes exceed cap."""
    huge = b"\x89PNG" + (b"\x00" * (11 * 1024 * 1024))  # > 10 MB default cap
    mock_reader = AsyncMock(return_value=huge)
    with patch("mcp_clipboard.clipboard._get_backend", return_value="wayland"):
        with patch.dict("mcp_clipboard.clipboard._IMAGE_READERS", {"wayland": mock_reader}):
            with pytest.raises(ClipboardSizeError, match="exceeds clipboard read limit"):
                await read_clipboard_image("image/png")


@pytest.mark.asyncio
async def test_paste_image_format_unknown_subtype_falls_back_to_png():
    """A subtype outside the allowlist must not flow into Image(format=...)."""
    fake_data = b"\x89PNG\x00\x00\x00"
    # x-icon is a real image MIME but not in _IMAGE_SUBTYPE_ALLOWLIST; this
    # exercises the explicit `fmt = "png"` fallback. (Parameter forms like
    # 'image/png; injected="oops"' get stripped by base_mime_type before
    # subtype extraction, so they never reach the fallback assignment.)
    weird_mime = "image/x-icon"
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=[weird_mime]):
            with patch("mcp_clipboard.server.read_clipboard_image", return_value=fake_data):
                result = await clipboard_paste()

    assert isinstance(result, Image)
    assert result._format == "png"


@pytest.mark.asyncio
async def test_paste_image_format_strips_mime_parameters():
    """MIME parameters (e.g. ;charset=...) must be stripped before subtype check."""
    fake_data = b"\x89PNG\x00\x00\x00"
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch(
            "mcp_clipboard.server.list_clipboard_formats",
            return_value=['image/png; injected="oops"'],
        ):
            with patch("mcp_clipboard.server.read_clipboard_image", return_value=fake_data):
                result = await clipboard_paste()

    assert isinstance(result, Image)
    assert result._format == "png"


@pytest.mark.asyncio
async def test_paste_image_format_uppercase_subtype_normalizes():
    """Image subtype is case-insensitive when matching the allowlist."""
    fake_data = b"GIF89a"
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=["image/GIF"]):
            with patch("mcp_clipboard.server.read_clipboard_image", return_value=fake_data):
                result = await clipboard_paste()

    assert isinstance(result, Image)
    assert result._format == "gif"


@pytest.mark.asyncio
async def test_paste_image_too_large_returns_message():
    """clipboard_paste surfaces an explanatory message when image exceeds cap."""
    err = ClipboardSizeError(
        "Image exceeds clipboard read limit (12,000,000 bytes, max 10,485,760)."
    )
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=["image/png"]):
            with patch("mcp_clipboard.server.read_clipboard_image", side_effect=err):
                result = await clipboard_paste()

    assert isinstance(result, str)
    assert "too large" in result.lower()


# ---------------------------------------------------------------------------
# 15. Image read backends
# ---------------------------------------------------------------------------

from mcp_clipboard.clipboard import (
    _macos_read_image,
    _wayland_read_image,
    _windows_read_image,
    _x11_read_image,
)


@pytest.mark.asyncio
async def test_wayland_read_image():
    """_wayland_read_image calls wl-paste with correct type flag."""
    fake_data = b"\x89PNG\r\n\x1a\n"
    with patch(
        "mcp_clipboard.clipboard._run_binary", new_callable=AsyncMock, return_value=fake_data
    ) as mock:
        result = await _wayland_read_image("image/png")

    assert result == fake_data
    cmd = mock.call_args[0][0]
    assert cmd == ["wl-paste", "--type", "image/png"]


@pytest.mark.asyncio
async def test_x11_read_image():
    """_x11_read_image calls xclip with correct target for binary."""
    fake_data = b"\x89PNG\r\n\x1a\n"
    with patch(
        "mcp_clipboard.clipboard._run_binary", new_callable=AsyncMock, return_value=fake_data
    ) as mock:
        result = await _x11_read_image("image/png")

    assert result == fake_data
    cmd = mock.call_args[0][0]
    assert cmd == ["xclip", "-selection", "clipboard", "-target", "image/png", "-o"]


@pytest.mark.asyncio
async def test_macos_read_image():
    """_macos_read_image reads base64 from osascript and decodes."""
    import base64

    fake_data = b"\x89PNG\r\n\x1a\n"
    b64_text = base64.b64encode(fake_data).decode()
    with patch("mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value=b64_text):
        result = await _macos_read_image("image/png")

    assert result == fake_data


@pytest.mark.asyncio
async def test_macos_read_image_empty():
    """_macos_read_image returns empty bytes when no image available."""
    with patch("mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value=""):
        result = await _macos_read_image("image/png")

    assert result == b""


@pytest.mark.asyncio
async def test_windows_read_image():
    """_windows_read_image reads base64 from PowerShell and decodes."""
    import base64

    fake_data = b"\x89PNG\r\n\x1a\n"
    b64_text = base64.b64encode(fake_data).decode()
    with patch("mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value=b64_text):
        result = await _windows_read_image("image/png")

    assert result == fake_data


@pytest.mark.asyncio
async def test_read_clipboard_image_dispatches():
    """read_clipboard_image dispatches to the correct backend."""
    mock_reader = AsyncMock(return_value=b"IMG")
    with patch("mcp_clipboard.clipboard._get_backend", return_value="x11"):
        with patch.dict("mcp_clipboard.clipboard._IMAGE_READERS", {"x11": mock_reader}):
            result = await read_clipboard_image("image/png")

    assert result == b"IMG"
    mock_reader.assert_called_once_with("image/png", "clipboard")


# ---------------------------------------------------------------------------
# 16. Clipboard copy
# ---------------------------------------------------------------------------

from mcp_clipboard.clipboard import (
    _macos_write,
    _wayland_write,
    _x11_write,
)


@pytest.mark.asyncio
async def test_clipboard_copy_success():
    """clipboard_copy writes content and returns confirmation."""
    with patch("mcp_clipboard.server.write_clipboard", new_callable=AsyncMock):
        result = await clipboard_copy("hello world")

    assert "11 characters" in result


@pytest.mark.asyncio
async def test_clipboard_copy_error():
    """clipboard_copy returns error message on failure."""
    with patch("mcp_clipboard.server.write_clipboard", side_effect=ClipboardError("write failed")):
        result = await clipboard_copy("hello")

    assert "Error" in result
    assert "write failed" in result


@pytest.mark.asyncio
async def test_wayland_write():
    """_wayland_write pipes content to wl-copy."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _wayland_write("hello")

    cmd = mock.call_args[0][0]
    data = mock.call_args[0][1]
    assert cmd == ["wl-copy"]
    assert data == b"hello"


@pytest.mark.asyncio
async def test_x11_write():
    """_x11_write pipes content to xclip."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _x11_write("hello")

    cmd = mock.call_args[0][0]
    data = mock.call_args[0][1]
    assert cmd == ["xclip", "-selection", "clipboard"]
    assert data == b"hello"


@pytest.mark.asyncio
async def test_macos_write():
    """_macos_write pipes content to pbcopy."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write("hello")

    cmd = mock.call_args[0][0]
    data = mock.call_args[0][1]
    assert cmd == ["pbcopy"]
    assert data == b"hello"


@pytest.mark.asyncio
async def test_windows_write_dispatches_to_win32_wrapper():
    """_windows_write forwards plain text to clipboard_win32.write_text
    with mime_type='text/plain' (CF_UNICODETEXT under the hood)."""
    with patch("mcp_clipboard.clipboard_win32.write_text") as mock_write:
        await _windows_write("hello")

    mock_write.assert_called_once_with("hello", "text/plain")


@pytest.mark.asyncio
async def test_windows_write_wraps_win32_errors_as_clipboard_error():
    """Errors from the win32 wrapper surface as ClipboardError."""
    with patch(
        "mcp_clipboard.clipboard_win32.write_text",
        side_effect=RuntimeError("OpenClipboard failed"),
    ):
        with pytest.raises(ClipboardError, match="Windows clipboard write failed"):
            await _windows_write("hello")


@pytest.mark.asyncio
async def test_write_clipboard_dispatches():
    """write_clipboard dispatches to the correct backend."""
    mock_writer = AsyncMock()
    with patch("mcp_clipboard.clipboard._get_backend", return_value="wayland"):
        with patch.dict("mcp_clipboard.clipboard._WRITERS", {"wayland": mock_writer}):
            await write_clipboard("hello")

    mock_writer.assert_called_once_with("hello")


# ---------------------------------------------------------------------------
# MIME type parameter handling
# ---------------------------------------------------------------------------

from mcp_clipboard.clipboard import base_mime_type


def test_base_mime_type_strips_params():
    """base_mime_type strips everything after the semicolon."""
    assert base_mime_type("text/plain;charset=utf-8") == "text/plain"
    assert base_mime_type('image/svg+xml;windows_formatname="image/svg+xml"') == "image/svg+xml"
    assert base_mime_type("text/plain") == "text/plain"
    assert base_mime_type("application/json") == "application/json"


@pytest.mark.asyncio
async def test_read_clipboard_falls_back_to_suffixed_mime():
    """read_clipboard retries with the suffixed MIME type when exact match fails."""
    call_log = []

    async def mock_reader(mime_type, selection="clipboard"):
        call_log.append(mime_type)
        if mime_type == "text/plain":
            return ""  # exact match fails
        if mime_type == "text/plain;charset=utf-8":
            return "hello from charset"
        return ""

    async def mock_list(selection="clipboard"):
        return ["text/plain;charset=utf-8", "text/html"]

    with patch("mcp_clipboard.clipboard._get_backend", return_value="wayland"):
        with patch.dict("mcp_clipboard.clipboard._READERS", {"wayland": mock_reader}):
            with patch.dict("mcp_clipboard.clipboard._FORMAT_LISTERS", {"wayland": mock_list}):
                result = await read_clipboard("text/plain")

    assert result == "hello from charset"
    assert "text/plain" in call_log
    assert "text/plain;charset=utf-8" in call_log


@pytest.mark.asyncio
async def test_read_clipboard_no_fallback_when_exact_match_works():
    """read_clipboard does not list formats when the exact MIME type succeeds."""
    list_called = False

    async def mock_reader(mime_type, selection="clipboard"):
        return "direct content"

    async def mock_list(selection="clipboard"):
        nonlocal list_called
        list_called = True
        return []

    with patch("mcp_clipboard.clipboard._get_backend", return_value="wayland"):
        with patch.dict("mcp_clipboard.clipboard._READERS", {"wayland": mock_reader}):
            with patch.dict("mcp_clipboard.clipboard._FORMAT_LISTERS", {"wayland": mock_list}):
                result = await read_clipboard("text/plain")

    assert result == "direct content"
    assert not list_called


@pytest.mark.asyncio
async def test_read_clipboard_no_fallback_on_macos():
    """read_clipboard skips MIME fallback on macOS (not applicable)."""

    async def mock_reader(mime_type, selection="clipboard"):
        return ""

    list_called = False

    async def mock_list(selection="clipboard"):
        nonlocal list_called
        list_called = True
        return ["text/plain;charset=utf-8"]

    with patch("mcp_clipboard.clipboard._get_backend", return_value="macos"):
        with patch.dict("mcp_clipboard.clipboard._READERS", {"macos": mock_reader}):
            with patch.dict("mcp_clipboard.clipboard._FORMAT_LISTERS", {"macos": mock_list}):
                result = await read_clipboard("text/plain")

    assert result == ""
    assert not list_called


@pytest.mark.asyncio
async def test_read_clipboard_image_falls_back_to_suffixed_mime():
    """read_clipboard_image retries with suffixed MIME type on fallback."""

    async def mock_reader(mime_type, selection="clipboard"):
        if mime_type == "image/png":
            return b""
        if mime_type == "image/png;charset=binary":
            return b"\x89PNG"
        return b""

    async def mock_list(selection="clipboard"):
        return ["image/png;charset=binary"]

    with patch("mcp_clipboard.clipboard._get_backend", return_value="x11"):
        with patch.dict("mcp_clipboard.clipboard._IMAGE_READERS", {"x11": mock_reader}):
            with patch.dict("mcp_clipboard.clipboard._FORMAT_LISTERS", {"x11": mock_list}):
                result = await read_clipboard_image("image/png")

    assert result == b"\x89PNG"


@pytest.mark.asyncio
async def test_read_raw_allows_svg_with_params():
    """clipboard_read_raw allows image/svg+xml with parameter suffix."""
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="50"/></svg>'
    with patch("mcp_clipboard.server.read_clipboard", new_callable=AsyncMock, return_value=svg):
        result = await clipboard_read_raw(
            mime_type='image/svg+xml;windows_formatname="image/svg+xml"'
        )

    assert "circle" in result
    assert "Cannot read binary" not in result


@pytest.mark.asyncio
async def test_read_raw_rejects_binary_with_params():
    """clipboard_read_raw still rejects binary MIME types that have parameter suffixes."""
    result = await clipboard_read_raw(mime_type="image/png;charset=binary")

    assert "Cannot read binary" in result


@pytest.mark.asyncio
async def test_paste_image_prefers_png_with_params():
    """clipboard_paste prefers PNG even when format has parameter suffix."""
    fake_png = b"\x89PNG\r\n\x1a\n"
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch(
            "mcp_clipboard.server.list_clipboard_formats",
            return_value=["image/tiff", "image/png;charset=binary"],
        ):
            with patch(
                "mcp_clipboard.server.read_clipboard_image", return_value=fake_png
            ) as mock_img:
                result = await clipboard_paste()

    # Should use the suffixed PNG format, not fall back to tiff
    mock_img.assert_called_once_with("image/png;charset=binary", "clipboard")
    assert isinstance(result, Image)


@pytest.mark.asyncio
async def test_paste_text_with_suffixed_mime():
    """clipboard_paste returns text content when clipboard has text/plain;charset=utf-8.

    This is the core bug scenario: Claude Desktop puts text on the clipboard with
    MIME type text/plain;charset=utf-8, and clipboard_paste should still read it.
    """
    # read_clipboard already handles fallback resolution in clipboard.py,
    # so from server.py's perspective, the mock just returns the content.
    with patch(
        "mcp_clipboard.server.read_clipboard",
        side_effect=_mock_read(html="", text="Hello from Claude"),
    ):
        result = await clipboard_paste()

    assert "Hello from Claude" in result


# ---------------------------------------------------------------------------
# 18. _run_binary() error handling
# ---------------------------------------------------------------------------

from mcp_clipboard.clipboard import _run_binary


@pytest.mark.asyncio
async def test_run_binary_file_not_found():
    """_run_binary raises ClipboardError when the command is not found."""
    with pytest.raises(ClipboardError, match="Command not found: nonexistent_cmd"):
        await _run_binary(["nonexistent_cmd"])


@pytest.mark.asyncio
async def test_run_binary_timeout():
    """_run_binary raises ClipboardError on timeout."""
    with pytest.raises(ClipboardError, match="timed out"):
        await _run_binary(["sleep", "10"], timeout=0.1)


@pytest.mark.asyncio
async def test_run_subprocess_awaits_wait_after_kill_on_timeout():
    """_run_subprocess calls await proc.wait() after kill() when timing out."""
    from mcp_clipboard.clipboard import _run_subprocess

    class FakeProc:
        def __init__(self):
            self.kill_called = False
            self.wait_called = False
            self.returncode = -9

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            self.kill_called = True

        async def wait(self):
            self.wait_called = True
            return self.returncode

    fake = FakeProc()

    async def fake_create(*_args, **_kwargs):
        return fake

    with patch("mcp_clipboard.clipboard.asyncio.create_subprocess_exec", fake_create):
        with pytest.raises(ClipboardError, match="timed out"):
            await _run_subprocess(["sleep", "10"], timeout=0.05)

    assert fake.kill_called, "kill() must be called on timeout"
    assert fake.wait_called, "wait() must be awaited after kill() to reap the process"


@pytest.mark.asyncio
async def test_run_subprocess_bounded_wait_when_child_is_wedged():
    """If the child hangs in wait() after kill(), _run_subprocess must not block indefinitely.

    Mirrors a wedged-child scenario: wait() never returns. The bounded
    ``asyncio.wait_for(proc.wait(), timeout=1.0)`` guarantees forward progress.
    """
    from mcp_clipboard.clipboard import _run_subprocess

    class WedgedProc:
        def __init__(self):
            self.returncode = None

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            pass

        async def wait(self):
            await asyncio.sleep(10)  # simulates a wedged child
            return -9

    async def fake_create(*_args, **_kwargs):
        return WedgedProc()

    loop = asyncio.get_running_loop()
    start = loop.time()
    with patch("mcp_clipboard.clipboard.asyncio.create_subprocess_exec", fake_create):
        with pytest.raises(ClipboardError, match="timed out"):
            await _run_subprocess(["sleep", "10"], timeout=0.05)
    elapsed = loop.time() - start

    # Outer 0.05s timeout + inner 1.0s bounded wait ≈ ≤ 1.1s; leave generous headroom.
    assert elapsed < 2.5, f"bounded wait not enforced: elapsed {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_run_subprocess_kills_on_cancellation():
    """asyncio.CancelledError must trigger proc.kill() to avoid orphans.

    CancelledError inherits from BaseException, not Exception, so a bare
    `except Exception` would skip it. The fix relies on a `finally` block.
    """
    from mcp_clipboard.clipboard import _run_subprocess

    class FakeProc:
        def __init__(self):
            self.kill_called = False
            self.returncode: int | None = None

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            self.kill_called = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    fake = FakeProc()

    async def fake_create(*_args, **_kwargs):
        return fake

    async def runner():
        with patch("mcp_clipboard.clipboard.asyncio.create_subprocess_exec", fake_create):
            await _run_subprocess(["sleep", "10"], timeout=10.0)

    task = asyncio.create_task(runner())
    await asyncio.sleep(0.05)  # let the task enter communicate()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake.kill_called, "kill() must be called when the task is canceled"


@pytest.mark.asyncio
async def test_run_with_stdin_kills_on_cancellation():
    """asyncio.CancelledError must trigger proc.kill() in _run_with_stdin too."""
    from mcp_clipboard.clipboard import _run_with_stdin

    class FakeProc:
        def __init__(self):
            self.kill_called = False
            self.returncode: int | None = None

        async def communicate(self, input=None):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            self.kill_called = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    fake = FakeProc()

    async def fake_create(*_args, **_kwargs):
        return fake

    async def runner():
        with patch("mcp_clipboard.clipboard.asyncio.create_subprocess_exec", fake_create):
            await _run_with_stdin(["sleep", "10"], b"data", timeout=10.0)

    task = asyncio.create_task(runner())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake.kill_called, "kill() must be called when the task is canceled"


@pytest.mark.asyncio
async def test_run_binary_exit_code_1_returns_empty():
    """_run_binary returns empty bytes for exit code 1 (format not available)."""
    result = await _run_binary(["sh", "-c", "exit 1"])
    assert result == b""


@pytest.mark.asyncio
async def test_run_binary_exit_code_2_raises():
    """_run_binary raises ClipboardError for exit codes > 1."""
    with pytest.raises(ClipboardError, match="rc=2"):
        await _run_binary(["sh", "-c", "echo err >&2; exit 2"])


@pytest.mark.asyncio
async def test_run_binary_returns_raw_bytes():
    """_run_binary returns raw bytes from stdout.

    Uses /usr/bin/env python3 with -c rather than printf because BSD
    printf (macOS, BSDs) does not expand \\xNN escape sequences -- it
    emits the literal characters -- whereas GNU printf (Linux) does.
    Python's sys.stdout.buffer.write is portable across all platforms.
    Skipped on Windows where /usr/bin/env is not present.
    """
    import sys as _sys

    if _sys.platform == "win32":
        import pytest as _pytest

        _pytest.skip("python -c stdout-bytes test does not need a Windows variant")
    result = await _run_binary(
        ["python3", "-c", "import sys; sys.stdout.buffer.write(b'\\x89PNG')"]
    )
    assert result == b"\x89PNG"


# ---------------------------------------------------------------------------
# 19. _run_with_stdin() error handling
# ---------------------------------------------------------------------------

from mcp_clipboard.clipboard import _run_with_stdin


@pytest.mark.asyncio
async def test_run_with_stdin_file_not_found():
    """_run_with_stdin raises ClipboardError when the command is not found."""
    with pytest.raises(ClipboardError, match="Command not found: nonexistent_cmd"):
        await _run_with_stdin(["nonexistent_cmd"], b"data")


@pytest.mark.asyncio
async def test_run_with_stdin_timeout():
    """_run_with_stdin raises ClipboardError on timeout."""
    with pytest.raises(ClipboardError, match="timed out"):
        await _run_with_stdin(["sleep", "10"], b"data", timeout=0.1)


@pytest.mark.asyncio
async def test_run_with_stdin_awaits_wait_after_kill_on_timeout():
    """_run_with_stdin calls await proc.wait() after kill() when timing out."""

    class FakeProc:
        def __init__(self):
            self.kill_called = False
            self.wait_called = False
            self.returncode = -9

        async def communicate(self, input=None):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            self.kill_called = True

        async def wait(self):
            self.wait_called = True
            return self.returncode

    fake = FakeProc()

    async def fake_create(*_args, **_kwargs):
        return fake

    with patch("mcp_clipboard.clipboard.asyncio.create_subprocess_exec", fake_create):
        with pytest.raises(ClipboardError, match="timed out"):
            await _run_with_stdin(["sleep", "10"], b"data", timeout=0.05)

    assert fake.kill_called, "kill() must be called on timeout"
    assert fake.wait_called, "wait() must be awaited after kill() to reap the process"


@pytest.mark.asyncio
async def test_run_with_stdin_bounded_wait_when_child_is_wedged():
    """If the child hangs in wait() after kill(), _run_with_stdin must not block indefinitely."""

    class WedgedProc:
        def __init__(self):
            self.returncode = None

        async def communicate(self, input=None):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            pass

        async def wait(self):
            await asyncio.sleep(10)  # simulates a wedged child
            return -9

    async def fake_create(*_args, **_kwargs):
        return WedgedProc()

    loop = asyncio.get_running_loop()
    start = loop.time()
    with patch("mcp_clipboard.clipboard.asyncio.create_subprocess_exec", fake_create):
        with pytest.raises(ClipboardError, match="timed out"):
            await _run_with_stdin(["sleep", "10"], b"data", timeout=0.05)
    elapsed = loop.time() - start

    assert elapsed < 2.5, f"bounded wait not enforced: elapsed {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_run_with_stdin_nonzero_exit_raises():
    """_run_with_stdin raises ClipboardError on any non-zero exit code (including 1)."""
    with pytest.raises(ClipboardError, match="Clipboard write failed"):
        await _run_with_stdin(["sh", "-c", "exit 1"], b"data")


@pytest.mark.asyncio
async def test_run_with_stdin_success():
    """_run_with_stdin succeeds when command exits 0."""
    # cat reads stdin and writes to stdout (which is DEVNULL), exits 0
    await _run_with_stdin(["cat"], b"hello")


@pytest.mark.asyncio
async def test_run_with_stdin_includes_stderr_in_debug():
    """_run_with_stdin includes stderr in error message when debug is on."""
    with patch.dict("os.environ", {"MCP_CLIPBOARD_DEBUG": "1"}):
        with pytest.raises(ClipboardError, match=r"stderr:.*error output"):
            await _run_with_stdin(["sh", "-c", "echo 'error output' >&2; exit 1"], b"data")


@pytest.mark.asyncio
async def test_run_with_stdin_no_stderr_without_debug():
    """_run_with_stdin omits stderr from error message when debug is off."""
    with patch.dict("os.environ", {"MCP_CLIPBOARD_DEBUG": "0"}):
        with pytest.raises(ClipboardError, match="Clipboard write failed") as exc_info:
            await _run_with_stdin(["sh", "-c", "echo 'error output' >&2; exit 1"], b"data")
        assert "stderr:" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 20. clipboard_paste() image format and error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paste_image_format_field_png():
    """clipboard_paste sets Image.format to 'png' for image/png."""
    fake_png = b"\x89PNG\r\n\x1a\n"
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=["image/png"]):
            with patch("mcp_clipboard.server.read_clipboard_image", return_value=fake_png):
                result = await clipboard_paste()

    assert isinstance(result, Image)
    assert result._format == "png"


@pytest.mark.asyncio
async def test_paste_image_format_field_jpeg():
    """clipboard_paste sets Image.format to 'jpeg' for image/jpeg."""
    fake_jpeg = b"\xff\xd8\xff"
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=["image/jpeg"]):
            with patch("mcp_clipboard.server.read_clipboard_image", return_value=fake_jpeg):
                result = await clipboard_paste()

    assert isinstance(result, Image)
    assert result._format == "jpeg"


@pytest.mark.asyncio
async def test_paste_image_format_field_with_params():
    """clipboard_paste strips MIME params before extracting format."""
    fake_png = b"\x89PNG"
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch(
            "mcp_clipboard.server.list_clipboard_formats", return_value=["image/png;charset=binary"]
        ):
            with patch("mcp_clipboard.server.read_clipboard_image", return_value=fake_png):
                result = await clipboard_paste()

    assert isinstance(result, Image)
    assert result._format == "png"


@pytest.mark.asyncio
async def test_paste_list_formats_error_falls_through():
    """clipboard_paste handles ClipboardError from list_clipboard_formats gracefully."""
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch(
            "mcp_clipboard.server.list_clipboard_formats",
            side_effect=ClipboardError("no clipboard"),
        ):
            result = await clipboard_paste()

    assert isinstance(result, str)
    assert "empty" in result.lower() or "Clipboard" in result


@pytest.mark.asyncio
async def test_paste_mixed_image_and_audio():
    """clipboard_paste returns image when both image and audio formats are present."""
    fake_png = b"\x89PNG"
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch(
            "mcp_clipboard.server.list_clipboard_formats", return_value=["audio/mpeg", "image/png"]
        ):
            with patch("mcp_clipboard.server.read_clipboard_image", return_value=fake_png):
                result = await clipboard_paste()

    assert isinstance(result, Image)


@pytest.mark.asyncio
async def test_paste_video_reports_binary():
    """clipboard_paste reports video MIME types as unsupported binary."""
    with patch("mcp_clipboard.server.read_clipboard", _mock_read(html="", text="")):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=["video/mp4"]):
            result = await clipboard_paste()

    assert isinstance(result, str)
    assert "video/mp4" in result
    assert "binary data" in result


@pytest.mark.asyncio
async def test_paste_rtf_fallback():
    """clipboard_paste returns RTF content when HTML and plain text are empty."""
    rtf_content = r"{\rtf1\ansi Hello, {\b world}!}"

    async def mock_read(mime_type="text/plain", selection="clipboard"):
        if mime_type == "text/rtf":
            return rtf_content
        return ""

    with patch("mcp_clipboard.server.read_clipboard", side_effect=mock_read):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=["text/rtf"]):
            result = await clipboard_paste()

    assert isinstance(result, str)
    assert "rich text (RTF)" in result
    assert rtf_content in result


@pytest.mark.asyncio
async def test_paste_rtf_with_triple_backticks_uses_longer_fence():
    """RTF content containing ``` must not break out of the markdown fence."""
    rtf_content = r"{\rtf1\ansi look: ```escape```}"

    async def mock_read(mime_type="text/plain", selection="clipboard"):
        if mime_type == "text/rtf":
            return rtf_content
        return ""

    with patch("mcp_clipboard.server.read_clipboard", side_effect=mock_read):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=["text/rtf"]):
            result = await clipboard_paste()

    assert "```escape```" in result
    assert "````\n" in result


@pytest.mark.asyncio
async def test_paste_rtf_truncated():
    """clipboard_paste truncates oversized RTF at 50KB."""
    rtf_content = r"{\rtf1\ansi " + ("x" * 60_000) + "}"

    async def mock_read(mime_type="text/plain", selection="clipboard"):
        if mime_type == "text/rtf":
            return rtf_content
        return ""

    with patch("mcp_clipboard.server.read_clipboard", side_effect=mock_read):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=["text/rtf"]):
            result = await clipboard_paste()

    assert "truncated" in result
    assert "rich text (RTF)" in result


@pytest.mark.asyncio
async def test_paste_rtf_skipped_when_text_present():
    """clipboard_paste does not attempt RTF read when plain text is available."""

    async def mock_read(mime_type="text/plain", selection="clipboard"):
        if mime_type == "text/plain":
            return "hello world"
        if mime_type == "text/rtf":
            raise AssertionError("RTF should not be read when plain text is present")
        return ""

    with patch("mcp_clipboard.server.read_clipboard", side_effect=mock_read):
        result = await clipboard_paste()

    assert "hello world" in result


@pytest.mark.asyncio
async def test_paste_rtf_error_falls_through():
    """clipboard_paste falls through to binary check when RTF read raises ClipboardError."""

    async def mock_read(mime_type="text/plain", selection="clipboard"):
        if mime_type == "text/rtf":
            raise ClipboardError("rtf not available")
        return ""

    with patch("mcp_clipboard.server.read_clipboard", side_effect=mock_read):
        with patch("mcp_clipboard.server.list_clipboard_formats", return_value=[]):
            result = await clipboard_paste()

    assert "empty" in result.lower()


# ---------------------------------------------------------------------------
# 21. read_clipboard_image() fallback edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_clipboard_image_no_fallback_on_macos():
    """read_clipboard_image skips MIME fallback on macOS."""

    async def mock_reader(mime_type, selection="clipboard"):
        return b""

    list_called = False

    async def mock_list(selection="clipboard"):
        nonlocal list_called
        list_called = True
        return ["image/png;charset=binary"]

    with patch("mcp_clipboard.clipboard._get_backend", return_value="macos"):
        with patch.dict("mcp_clipboard.clipboard._IMAGE_READERS", {"macos": mock_reader}):
            with patch.dict("mcp_clipboard.clipboard._FORMAT_LISTERS", {"macos": mock_list}):
                result = await read_clipboard_image("image/png")

    assert result == b""
    assert not list_called


@pytest.mark.asyncio
async def test_read_clipboard_image_no_fallback_on_windows():
    """read_clipboard_image skips MIME fallback on Windows."""

    async def mock_reader(mime_type, selection="clipboard"):
        return b""

    list_called = False

    async def mock_list(selection="clipboard"):
        nonlocal list_called
        list_called = True
        return ["image/png;charset=binary"]

    with patch("mcp_clipboard.clipboard._get_backend", return_value="windows"):
        with patch.dict("mcp_clipboard.clipboard._IMAGE_READERS", {"windows": mock_reader}):
            with patch.dict("mcp_clipboard.clipboard._FORMAT_LISTERS", {"windows": mock_list}):
                result = await read_clipboard_image("image/png")

    assert result == b""
    assert not list_called


@pytest.mark.asyncio
async def test_read_clipboard_image_fallback_no_match():
    """read_clipboard_image returns empty bytes when fallback finds no matching base type."""

    async def mock_reader(mime_type, selection="clipboard"):
        return b""

    async def mock_list(selection="clipboard"):
        return ["image/tiff", "text/plain"]

    with patch("mcp_clipboard.clipboard._get_backend", return_value="wayland"):
        with patch.dict("mcp_clipboard.clipboard._IMAGE_READERS", {"wayland": mock_reader}):
            with patch.dict("mcp_clipboard.clipboard._FORMAT_LISTERS", {"wayland": mock_list}):
                result = await read_clipboard_image("image/png")

    assert result == b""


# ---------------------------------------------------------------------------
# 22. write_clipboard() dispatch to all backends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_clipboard_dispatches_to_x11():
    """write_clipboard dispatches to x11 backend."""
    mock_writer = AsyncMock()
    with patch("mcp_clipboard.clipboard._get_backend", return_value="x11"):
        with patch.dict("mcp_clipboard.clipboard._WRITERS", {"x11": mock_writer}):
            await write_clipboard("test")

    mock_writer.assert_called_once_with("test")


@pytest.mark.asyncio
async def test_write_clipboard_dispatches_to_macos():
    """write_clipboard dispatches to macos backend."""
    mock_writer = AsyncMock()
    with patch("mcp_clipboard.clipboard._get_backend", return_value="macos"):
        with patch.dict("mcp_clipboard.clipboard._WRITERS", {"macos": mock_writer}):
            await write_clipboard("test")

    mock_writer.assert_called_once_with("test")


@pytest.mark.asyncio
async def test_write_clipboard_dispatches_to_windows():
    """write_clipboard dispatches to windows backend."""
    mock_writer = AsyncMock()
    with patch("mcp_clipboard.clipboard._get_backend", return_value="windows"):
        with patch.dict("mcp_clipboard.clipboard._WRITERS", {"windows": mock_writer}):
            await write_clipboard("test")

    mock_writer.assert_called_once_with("test")


# ---------------------------------------------------------------------------
# 23. clipboard_read_raw allowlist/rejection edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_raw_allows_application_xml():
    """clipboard_read_raw allows application/xml as text-readable."""
    xml_str = '<?xml version="1.0"?><root/>'
    with patch("mcp_clipboard.server.read_clipboard", new_callable=AsyncMock, return_value=xml_str):
        result = await clipboard_read_raw(mime_type="application/xml")

    assert "root" in result
    assert "Cannot read binary" not in result


@pytest.mark.asyncio
async def test_read_raw_allows_application_xhtml():
    """clipboard_read_raw allows application/xhtml+xml as text-readable."""
    xhtml = '<html xmlns="http://www.w3.org/1999/xhtml"><body>hi</body></html>'
    with patch("mcp_clipboard.server.read_clipboard", new_callable=AsyncMock, return_value=xhtml):
        result = await clipboard_read_raw(mime_type="application/xhtml+xml")

    assert "hi" in result
    assert "Cannot read binary" not in result


@pytest.mark.asyncio
async def test_read_raw_rejects_octet_stream():
    """clipboard_read_raw rejects application/octet-stream."""
    result = await clipboard_read_raw(mime_type="application/octet-stream")

    assert "Cannot read binary" in result


@pytest.mark.asyncio
async def test_read_raw_rejects_numeric_mime():
    """clipboard_read_raw rejects MIME types starting with digits."""
    result = await clipboard_read_raw(mime_type="123/456")
    assert "Invalid MIME type" in result


@pytest.mark.asyncio
async def test_read_raw_rejects_underscore_mime():
    """clipboard_read_raw rejects MIME types like _/_."""
    result = await clipboard_read_raw(mime_type="_/_")
    assert "Invalid MIME type" in result


@pytest.mark.asyncio
async def test_read_raw_accepts_custom_mime():
    """clipboard_read_raw should accept valid custom MIME types."""
    # This should pass validation (but return empty since it's not on clipboard)
    with patch("mcp_clipboard.server.read_clipboard", new_callable=AsyncMock, return_value=""):
        result = await clipboard_read_raw(mime_type="application/x-custom")
    assert "Invalid MIME type" not in result


# ---------------------------------------------------------------------------
# 24. clipboard_copy() edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clipboard_copy_empty_string():
    """clipboard_copy handles empty string input."""
    with patch("mcp_clipboard.server.write_clipboard", new_callable=AsyncMock):
        result = await clipboard_copy("")

    assert "0 characters" in result


@pytest.mark.asyncio
async def test_clipboard_copy_unicode():
    """clipboard_copy handles unicode content correctly."""
    with patch("mcp_clipboard.server.write_clipboard", new_callable=AsyncMock) as mock:
        result = await clipboard_copy("Hello \U0001f30d \u4f60\u597d")

    mock.assert_called_once_with("Hello \U0001f30d \u4f60\u597d")
    assert "characters" in result


@pytest.mark.asyncio
async def test_clipboard_copy_rejects_oversized():
    """clipboard_copy rejects content exceeding the write limit."""
    with patch("mcp_clipboard.server._MAX_WRITE_BYTES", 10):
        result = await clipboard_copy("x" * 100)
    assert "exceeds clipboard write limit" in result


@pytest.mark.asyncio
async def test_clipboard_copy_at_limit():
    """clipboard_copy allows content exactly at the write limit."""
    with patch("mcp_clipboard.server._MAX_WRITE_BYTES", 5):
        with patch("mcp_clipboard.server.write_clipboard", new_callable=AsyncMock):
            result = await clipboard_copy("hello")  # 5 bytes
    assert "characters" in result


@pytest.mark.asyncio
async def test_clipboard_copy_rejects_invalid_mime_type():
    """clipboard_copy rejects MIME types that don't match _MIME_RE."""
    result = await clipboard_copy("hello", mime_type="not-a-mime")
    assert "Invalid MIME type" in result


@pytest.mark.asyncio
async def test_clipboard_copy_rejects_numeric_mime_type():
    """clipboard_copy rejects MIME types with numeric-prefixed type/subtype."""
    result = await clipboard_copy("hello", mime_type="123/456")
    assert "Invalid MIME type" in result


@pytest.mark.asyncio
async def test_clipboard_copy_accepts_valid_typed_mime():
    """clipboard_copy accepts valid non-text/plain MIME types."""
    with patch("mcp_clipboard.server.write_clipboard_typed", new_callable=AsyncMock):
        result = await clipboard_copy("hello", mime_type="text/html")
    assert "characters" in result
    assert "text/html" in result


# ---------------------------------------------------------------------------
# 25. Misc medium-priority tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_windows_read_image_empty():
    """_windows_read_image returns empty bytes when no image on clipboard."""
    with patch("mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value=""):
        result = await _windows_read_image("image/png")

    assert result == b""


@pytest.mark.asyncio
async def test_windows_read_image_jpeg():
    """_windows_read_image uses Jpeg format for image/jpeg."""
    import base64

    fake_data = b"\xff\xd8\xff"
    b64_text = base64.b64encode(fake_data).decode()
    with patch(
        "mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value=b64_text
    ) as mock_run:
        result = await _windows_read_image("image/jpeg")

    assert result == fake_data
    script = mock_run.call_args[0][0][-1]
    assert "ImageFormat]::Jpeg" in script


@pytest.mark.asyncio
async def test_windows_read_image_unsupported():
    """_windows_read_image rejects unsupported MIME types."""
    with pytest.raises(ClipboardError, match="Unsupported image type"):
        await _windows_read_image("image/webp")


@pytest.mark.asyncio
async def test_wayland_write_passes_env():
    """_wayland_write passes env from _wayland_env() to _run_with_stdin."""
    fake_env = {"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000"}
    with patch("mcp_clipboard.clipboard._wayland_env", return_value=fake_env):
        with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
            await _wayland_write("hello")

    assert mock.call_args.kwargs["env"] == fake_env


@pytest.mark.asyncio
async def test_macos_read_image_jpeg_uti():
    """_macos_read_image maps image/jpeg to public.jpeg UTI."""
    import base64

    fake_data = b"\xff\xd8\xff"
    b64_text = base64.b64encode(fake_data).decode()
    with patch(
        "mcp_clipboard.clipboard._run", new_callable=AsyncMock, return_value=b64_text
    ) as mock_run:
        result = await _macos_read_image("image/jpeg")

    assert result == fake_data
    # Verify the osascript uses the correct UTI
    script = mock_run.call_args[0][0][-1]
    assert "public.jpeg" in script


@pytest.mark.asyncio
async def test_macos_read_image_unknown_mime_rejected():
    """_macos_read_image rejects MIME types without a known UTI mapping."""
    with pytest.raises(ClipboardError, match="Unsupported image type"):
        await _macos_read_image("image/webp")


@pytest.mark.asyncio
async def test_macos_read_image_injection_rejected():
    """_macos_read_image rejects MIME types that could escape the AppleScript string."""
    with pytest.raises(ClipboardError, match="Unsupported image type"):
        await _macos_read_image('image/png"; -- ')


def test_load_instruction_clipboard_copy():
    """_load_instruction loads the clipboard_copy instruction file."""
    result = _load_instruction("clipboard_copy")
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_detect_backend_x11_fallback():
    """_detect_backend falls back to x11 when wl-paste is unavailable on Linux."""

    def mock_which(cmd):
        if cmd == "xclip":
            return "/usr/bin/xclip"
        return None  # wl-paste not found

    with patch("mcp_clipboard.clipboard.platform.system", return_value="Linux"):
        with patch("mcp_clipboard.clipboard.shutil.which", side_effect=mock_which):
            with patch.dict("os.environ", {}, clear=True):
                result = _detect_backend()

    assert result == "x11"


# ---------------------------------------------------------------------------
# 26. Typed write — platform backends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wayland_write_typed_plain():
    """_wayland_write_typed passes --type for text/plain."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _wayland_write_typed("hello", "text/plain")

    cmd = mock.call_args[0][0]
    assert "--type" in cmd
    assert "text/plain" in cmd
    assert mock.call_args[0][1] == b"hello"


@pytest.mark.asyncio
async def test_wayland_write_typed_html():
    """_wayland_write_typed passes --type text/html."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _wayland_write_typed("<b>hi</b>", "text/html")

    cmd = mock.call_args[0][0]
    assert "--type" in cmd
    assert "text/html" in cmd
    assert mock.call_args[0][1] == b"<b>hi</b>"


@pytest.mark.asyncio
async def test_x11_write_typed_plain():
    """_x11_write_typed passes -target for text/plain."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _x11_write_typed("hello", "text/plain")

    cmd = mock.call_args[0][0]
    assert "-target" in cmd
    assert "text/plain" in cmd
    assert mock.call_args[0][1] == b"hello"


@pytest.mark.asyncio
async def test_x11_write_typed_html():
    """_x11_write_typed passes -target text/html."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _x11_write_typed("<p>hello</p>", "text/html")

    cmd = mock.call_args[0][0]
    assert "-target" in cmd
    assert "text/html" in cmd


@pytest.mark.asyncio
async def test_macos_write_typed_plain():
    """_macos_write_typed uses pbcopy for text/plain."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_typed("hello", "text/plain")

    cmd = mock.call_args[0][0]
    assert cmd == ["pbcopy"]
    assert mock.call_args[0][1] == b"hello"


@pytest.mark.asyncio
async def test_macos_write_typed_html():
    """_macos_write_typed pipes osascript via stdin (`osascript -`) for text/html."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_typed("<b>hello</b>", "text/html")

    cmd = mock.call_args[0][0]
    assert cmd == ["osascript", "-"]
    script = mock.call_args[0][1].decode("utf-8")
    assert "public.html" in script
    assert "NSPasteboard" in script


@pytest.mark.asyncio
async def test_macos_write_typed_rtf():
    """_macos_write_typed uses public.rtf UTI for text/rtf."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_typed(r"{\rtf1 hello}", "text/rtf")

    script = mock.call_args[0][1].decode("utf-8")
    assert "public.rtf" in script


@pytest.mark.asyncio
async def test_macos_write_typed_unsupported():
    """_macos_write_typed raises ClipboardError for unsupported MIME types."""
    with pytest.raises(ClipboardError, match="macOS"):
        await _macos_write_typed("data", "text/csv")


@pytest.mark.asyncio
async def test_macos_write_typed_empty_content():
    """Empty HTML content must still produce a valid (empty-b64) AppleScript."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_typed("", "text/html")

    script = mock.call_args[0][1].decode("utf-8")
    # range(0, 0, _APPLESCRIPT_CHUNK) is empty, so the chunk-list fallback
    # to [""] must keep the script syntactically valid.
    assert 'set b64 to ""' in script
    assert "public.html" in script


@pytest.mark.asyncio
async def test_macos_write_typed_large_content_chunks_base64():
    """Large HTML content must split base64 across multiple AppleScript
    statements so no single line exceeds AppleScript's 32,767-char per-line
    parser limit. The limit applies to script source even when piped over
    stdin, so chunking is preserved post-#113."""
    # 100 KB of HTML -> ~133 KB base64; well over the per-line cap
    large_html = "<p>" + ("X" * 100_000) + "</p>"
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_typed(large_html, "text/html")

    script = mock.call_args[0][1].decode("utf-8")
    # Split into AppleScript source lines and check each stays under the limit.
    for line in script.split("\n"):
        assert len(line) < 32_767, f"AppleScript line exceeds limit: {len(line)} chars"
    # Concatenation must be present (proves the chunking happened).
    assert "set b64 to b64 &" in script


@pytest.mark.asyncio
async def test_macos_write_typed_command_line_bounded_for_large_payloads():
    """Regression for #113: macOS osascript -e <script> would blow ARG_MAX
    (~1 MiB) for HTML/RTF/SVG content above ~750 KB. After the fix, the
    script flows over stdin via `osascript -`, so argv stays a fixed ~13
    chars regardless of payload size."""
    big = "<p>" + ("X" * 1_000_000) + "</p>"  # 1 MB+
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_typed(big, "text/html")

    cmd = mock.call_args[0][0]
    cmdline_len = sum(len(arg) for arg in cmd) + max(0, len(cmd) - 1)
    assert cmdline_len < 1_048_576, (
        f"macOS osascript argv is {cmdline_len} chars; default ARG_MAX is "
        f"~1 MiB. Inputs of any size must use stdin via `osascript -`."
    )
    assert cmd == ["osascript", "-"]
    # The base64-decoded script (over stdin) must scale with the payload.
    assert len(mock.call_args[0][1]) > 1_000_000


@pytest.mark.asyncio
async def test_windows_write_typed_plain():
    """_windows_write_typed forwards text/plain to clipboard_win32.write_text
    unchanged (CF_UNICODETEXT under the hood)."""
    with patch("mcp_clipboard.clipboard_win32.write_text") as mock_write:
        await _windows_write_typed("hello", "text/plain")

    mock_write.assert_called_once_with("hello", "text/plain")


@pytest.mark.asyncio
async def test_windows_write_typed_html():
    """_windows_write_typed wraps text/html in CF_HTML before forwarding to
    clipboard_win32.write_text. The wrapper carries Version + StartHTML/
    EndHTML/StartFragment/EndFragment offsets so paste targets can find
    the body fragment in the surrounding boilerplate."""
    with patch("mcp_clipboard.clipboard_win32.write_text") as mock_write:
        await _windows_write_typed("<b>hello</b>", "text/html")

    payload, mime = mock_write.call_args[0]
    assert mime == "text/html"
    assert "Version:0.9" in payload
    assert "StartHTML:" in payload
    assert "StartFragment:" in payload
    assert "<!--StartFragment-->" in payload
    assert "<b>hello</b>" in payload


@pytest.mark.asyncio
async def test_windows_write_typed_rtf():
    """_windows_write_typed forwards text/rtf to clipboard_win32.write_text
    unchanged (the RTF source is the wire format)."""
    with patch("mcp_clipboard.clipboard_win32.write_text") as mock_write:
        await _windows_write_typed(r"{\rtf1 hi}", "text/rtf")

    mock_write.assert_called_once_with(r"{\rtf1 hi}", "text/rtf")


@pytest.mark.asyncio
async def test_windows_write_typed_unsupported():
    """_windows_write_typed raises ClipboardError for unsupported MIME types."""
    with pytest.raises(ClipboardError, match="Windows"):
        await _windows_write_typed("data", "text/csv")


# ---------------------------------------------------------------------------
# SVG write — typed-text path through clipboard_copy (#112)
# ---------------------------------------------------------------------------

_SAMPLE_SVG = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    '<rect width="10" height="10" fill="red"/>'
    "</svg>"
)


@pytest.mark.asyncio
async def test_clipboard_copy_accepts_svg_mime():
    """SVG must NOT be rejected as binary; it dispatches to typed-text write."""
    with patch("mcp_clipboard.server.write_clipboard_typed", new_callable=AsyncMock) as mock:
        result = await clipboard_copy(_SAMPLE_SVG, mime_type="image/svg+xml")

    mock.assert_called_once_with(_SAMPLE_SVG, "image/svg+xml")
    assert "image/svg+xml" in result
    assert "Cannot write binary" not in result


@pytest.mark.asyncio
async def test_clipboard_copy_image_png_still_rejected_as_binary():
    """clipboard_copy continues to reject true binary image MIME types and now
    points the host model at the dedicated image-write tool."""
    result = await clipboard_copy("data", mime_type="image/png")
    assert "Cannot write binary" in result
    assert "clipboard_copy_image" in result


@pytest.mark.asyncio
async def test_wayland_write_typed_svg():
    """_wayland_write_typed passes --type image/svg+xml through unchanged."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _wayland_write_typed(_SAMPLE_SVG, "image/svg+xml")

    cmd = mock.call_args[0][0]
    assert "--type" in cmd
    assert "image/svg+xml" in cmd
    assert mock.call_args[0][1] == _SAMPLE_SVG.encode()


@pytest.mark.asyncio
async def test_x11_write_typed_svg():
    """_x11_write_typed passes -target image/svg+xml through unchanged."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _x11_write_typed(_SAMPLE_SVG, "image/svg+xml")

    cmd = mock.call_args[0][0]
    assert "-target" in cmd
    assert "image/svg+xml" in cmd


@pytest.mark.asyncio
async def test_macos_write_typed_svg():
    """_macos_write_typed uses NSPasteboard with public.svg-image UTI."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_typed(_SAMPLE_SVG, "image/svg+xml")

    script = mock.call_args[0][1].decode("utf-8")
    assert "public.svg-image" in script
    assert "NSPasteboard" in script
    assert "setData" in script


@pytest.mark.asyncio
async def test_macos_write_typed_unsupported_lists_svg():
    """The unsupported-MIME error message advertises SVG as supported."""
    with pytest.raises(ClipboardError, match="image/svg\\+xml"):
        await _macos_write_typed("data", "text/csv")


@pytest.mark.asyncio
async def test_windows_write_typed_svg():
    """_windows_write_typed forwards image/svg+xml to clipboard_win32.write_text
    unchanged. The Win32 backend registers 'image/svg+xml' as a custom
    clipboard format ID and stores the markup as UTF-8 bytes."""
    with patch("mcp_clipboard.clipboard_win32.write_text") as mock_write:
        await _windows_write_typed(_SAMPLE_SVG, "image/svg+xml")

    mock_write.assert_called_once_with(_SAMPLE_SVG, "image/svg+xml")


@pytest.mark.asyncio
async def test_windows_write_typed_unsupported_lists_svg():
    """The unsupported-MIME error message advertises SVG as supported."""
    with pytest.raises(ClipboardError, match="image/svg\\+xml"):
        await _windows_write_typed("data", "text/csv")


# ---------------------------------------------------------------------------
# SVG round-trip read paths
#
# SVG is XML text but conventionally uses image/* MIME type, so it falls
# into a gap: dispatched as binary by `image/*` filters but rejected by
# raster image readers as "Unsupported image type". The fix routes SVG
# through a text-read branch on each backend and through a dedicated
# `clipboard_paste` SVG-fallback branch when no raster image is available.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_windows_read_svg_dispatches_to_win32_wrapper():
    """_windows_read for image/svg+xml forwards to clipboard_win32.read_text,
    which registers the 'image/svg+xml' custom format ID and reads the
    UTF-8-encoded markup. (Old test asserted PowerShell-script details that
    no longer exist; the underlying Win32 GetClipboardData call is exercised
    by integration tests against a real Windows clipboard.)"""
    from mcp_clipboard.clipboard import _windows_read

    with patch("mcp_clipboard.clipboard_win32.read_text", return_value=_SAMPLE_SVG) as mock_read:
        result = await _windows_read("image/svg+xml")

    assert result == _SAMPLE_SVG
    mock_read.assert_called_once_with("image/svg+xml")


@pytest.mark.asyncio
async def test_macos_read_svg_uses_public_svg_image_uti():
    """_macos_read for image/svg+xml reads the public.svg-image UTI as NSData
    and decodes UTF-8 to a string."""
    from mcp_clipboard.clipboard import _macos_read

    with patch("mcp_clipboard.clipboard._run", new=AsyncMock(return_value=_SAMPLE_SVG)) as mock:
        result = await _macos_read("image/svg+xml")

    assert result == _SAMPLE_SVG
    script = mock.call_args[0][0][2]
    assert 'dataForType:"public.svg-image"' in script
    assert "NSUTF8StringEncoding" in script


def test_macos_uti_to_mime_maps_public_svg_image():
    """list_clipboard_formats on macOS surfaces SVG as image/svg+xml, not as
    the raw public.svg-image UTI."""
    from mcp_clipboard.clipboard import _UTI_TO_MIME

    assert _UTI_TO_MIME.get("public.svg-image") == "image/svg+xml"


@pytest.mark.asyncio
async def test_clipboard_paste_returns_svg_as_fenced_text_when_only_svg_present():
    """When the clipboard has only image/svg+xml (no text, no raster image),
    clipboard_paste returns the SVG markup in an ```svg fenced code block."""
    with (
        patch(
            "mcp_clipboard.server.list_clipboard_formats",
            new=AsyncMock(return_value=["image/svg+xml"]),
        ),
        patch(
            "mcp_clipboard.server.read_clipboard",
            new=AsyncMock(
                side_effect=lambda mime, sel: _SAMPLE_SVG if mime == "image/svg+xml" else ""
            ),
        ),
    ):
        result = await clipboard_paste()

    assert isinstance(result, str)
    assert "```svg" in result
    assert _SAMPLE_SVG in result
    assert "Clipboard contains SVG" in result


@pytest.mark.asyncio
async def test_clipboard_paste_prefers_raster_over_svg_when_both_present():
    """When the clipboard has both image/png and image/svg+xml, clipboard_paste
    returns the rasterized PNG as an Image, not the SVG markup as text."""
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # plausible PNG-ish bytes

    with (
        patch("mcp_clipboard.server.read_clipboard", new=AsyncMock(return_value="")),
        patch(
            "mcp_clipboard.server.list_clipboard_formats",
            new=AsyncMock(return_value=["image/png", "image/svg+xml"]),
        ),
        patch(
            "mcp_clipboard.server.read_clipboard_image",
            new=AsyncMock(return_value=png_bytes),
        ),
    ):
        result = await clipboard_paste()

    assert isinstance(result, Image)


@pytest.mark.asyncio
async def test_clipboard_paste_truncates_oversized_svg():
    """SVG longer than _MAX_CONTENT_CHARS is truncated with a marker line,
    matching the RTF fallback's truncation behavior."""
    from mcp_clipboard.server import _MAX_CONTENT_CHARS

    big_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        + ("<rect/>" * (_MAX_CONTENT_CHARS // 7))
        + "</svg>"
    )
    assert len(big_svg) > _MAX_CONTENT_CHARS

    with (
        patch(
            "mcp_clipboard.server.list_clipboard_formats",
            new=AsyncMock(return_value=["image/svg+xml"]),
        ),
        patch(
            "mcp_clipboard.server.read_clipboard",
            new=AsyncMock(side_effect=lambda mime, sel: big_svg if mime == "image/svg+xml" else ""),
        ),
    ):
        result = await clipboard_paste()

    assert isinstance(result, str)
    assert "[truncated at" in result


@pytest.mark.asyncio
async def test_clipboard_paste_logs_and_falls_through_when_svg_read_errors():
    """If the SVG text read raises ClipboardError, the auto-dispatch logs and
    falls through to the binary-format check rather than crashing."""

    def _raise_on_svg(mime: str, sel: str) -> str:
        if mime == "image/svg+xml":
            raise ClipboardError("boom")
        return ""

    with (
        patch(
            "mcp_clipboard.server.list_clipboard_formats",
            new=AsyncMock(return_value=["image/svg+xml"]),
        ),
        patch("mcp_clipboard.server.read_clipboard", new=AsyncMock(side_effect=_raise_on_svg)),
    ):
        result = await clipboard_paste()

    # SVG read raised; no raster, no binary, so we end up at "Clipboard is empty."
    assert isinstance(result, str)
    assert "Clipboard is empty" in result


@pytest.mark.asyncio
async def test_clipboard_paste_does_not_route_svg_through_image_read_path():
    """Regression: the auto-dispatch must NOT call read_clipboard_image with
    'image/svg+xml'. That was the original Windows breakage -- the binary
    reader rejected SVG as 'Unsupported image type' and clipboard_paste
    silently returned 'Clipboard is empty'."""
    image_read = AsyncMock()  # would record the call if it happened

    with (
        patch(
            "mcp_clipboard.server.list_clipboard_formats",
            new=AsyncMock(return_value=["image/svg+xml"]),
        ),
        patch("mcp_clipboard.server.read_clipboard_image", new=image_read),
        patch(
            "mcp_clipboard.server.read_clipboard",
            new=AsyncMock(
                side_effect=lambda mime, sel: _SAMPLE_SVG if mime == "image/svg+xml" else ""
            ),
        ),
    ):
        await clipboard_paste()

    image_read.assert_not_called()


# ---------------------------------------------------------------------------
# Windows UTF-8 stdin encoding (#129)
#
# PowerShell's [Console]::In.ReadToEnd() decodes stdin using
# [Console]::InputEncoding, which defaults to the OEM/ANSI code page on
# Windows (commonly CP1252) -- not UTF-8. Writing UTF-8 bytes (Python's
# default .encode()) to that stream silently corrupts non-ASCII characters
# before Set-Clipboard ever sees them. Every Windows write script that
# pipes text content over stdin must explicitly set InputEncoding to UTF-8
# before reading.
# ---------------------------------------------------------------------------


# --- UTF-8 round-trip via the win32 wrapper --------------------------------
#
# The Windows backend no longer pipes content through PowerShell stdin, so
# the codepage-transcoding bug class that #131 closed (and #142 / #132
# closed on the read leg) cannot reoccur structurally. The
# clipboard_win32.write_text path takes a Python str and hands it to either
# CF_UNICODETEXT (UTF-16 native) or a custom-format-id slot encoded UTF-8;
# no console code page sits in the path. The tests below exercise the
# Python-side dispatch for non-ASCII content and confirm the bytes that
# would be handed to the Win32 layer are the correct UTF-8 sequences.


@pytest.mark.asyncio
async def test_windows_write_passes_str_for_non_ascii():
    """_windows_write hands the original Python str to the win32 wrapper.
    The wrapper internally writes CF_UNICODETEXT for text/plain so non-ASCII
    codepoints (em dash U+2014, curly quotes, ellipsis, CJK, Arabic, emoji)
    survive without codepage transliteration."""
    em_dash_text = "before — after"
    with patch("mcp_clipboard.clipboard_win32.write_text") as mock_write:
        await _windows_write(em_dash_text)

    mock_write.assert_called_once_with(em_dash_text, "text/plain")


@pytest.mark.parametrize(
    "mime_type,sample",
    [
        ("text/plain", "hello"),
        ("text/rtf", r"{\rtf1 hi}"),
        ("image/svg+xml", "<svg/>"),
    ],
)
@pytest.mark.asyncio
async def test_windows_write_typed_dispatch_passes_str_unchanged(mime_type, sample):
    """_windows_write_typed forwards the Python str to clipboard_win32.write_text
    unchanged for every supported MIME except text/html, which is wrapped
    in CF_HTML first (covered in test_windows_write_typed_html above)."""
    with patch("mcp_clipboard.clipboard_win32.write_text") as mock_write:
        await _windows_write_typed(sample, mime_type)

    mock_write.assert_called_once_with(sample, mime_type)


@pytest.mark.asyncio
async def test_windows_write_typed_wraps_win32_errors_as_clipboard_error():
    """Errors from the win32 wrapper surface as ClipboardError uniformly."""
    with patch(
        "mcp_clipboard.clipboard_win32.write_text",
        side_effect=RuntimeError("OpenClipboard failed"),
    ):
        with pytest.raises(ClipboardError, match="Windows clipboard write failed"):
            await _windows_write_typed("hi", "text/plain")


# ---------------------------------------------------------------------------
# 27. _windows_html_clipboard_wrap unit tests
# ---------------------------------------------------------------------------


def test_windows_html_clipboard_wrap_contains_header():
    result = _windows_html_clipboard_wrap("<p>Hello</p>")
    assert result.startswith("Version:0.9")
    assert "StartHTML:" in result
    assert "EndHTML:" in result
    assert "StartFragment:" in result
    assert "EndFragment:" in result


def test_windows_html_clipboard_wrap_contains_content():
    result = _windows_html_clipboard_wrap("<p>Hello</p>")
    assert "<p>Hello</p>" in result
    assert "<!--StartFragment-->" in result
    assert "<!--EndFragment-->" in result


def test_windows_html_clipboard_wrap_offsets_are_valid():
    """Byte offsets in the CF_HTML header must point to correct positions."""
    html = "<p>Test</p>"
    result = _windows_html_clipboard_wrap(html)
    result_bytes = result.encode("utf-8")

    import re

    start_html = int(re.search(r"StartHTML:(\d+)", result).group(1))
    end_html = int(re.search(r"EndHTML:(\d+)", result).group(1))
    start_frag = int(re.search(r"StartFragment:(\d+)", result).group(1))
    end_frag = int(re.search(r"EndFragment:(\d+)", result).group(1))

    assert result_bytes[start_html : start_html + 6] == b"<html>"
    assert result_bytes[start_frag : start_frag + len(html)] == html.encode("utf-8")
    assert end_html == start_html + len(result_bytes[start_html:])
    assert end_frag == start_frag + len(html.encode("utf-8"))


# ---------------------------------------------------------------------------
# 28. write_clipboard_typed dispatch + server clipboard_copy with mime_type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_clipboard_typed_dispatches():
    """write_clipboard_typed dispatches to the correct backend."""
    mock_writer = AsyncMock()
    with patch("mcp_clipboard.clipboard._get_backend", return_value="wayland"):
        with patch.dict("mcp_clipboard.clipboard._TYPED_WRITERS", {"wayland": mock_writer}):
            await write_clipboard_typed("<b>hi</b>", "text/html")

    mock_writer.assert_called_once_with("<b>hi</b>", "text/html")


@pytest.mark.asyncio
async def test_clipboard_copy_with_mime_type_html():
    """clipboard_copy with mime_type=text/html uses write_clipboard_typed."""
    with patch("mcp_clipboard.server.write_clipboard_typed", new_callable=AsyncMock) as mock:
        result = await clipboard_copy("<b>hello</b>", mime_type="text/html")

    mock.assert_called_once_with("<b>hello</b>", "text/html")
    assert "text/html" in result


@pytest.mark.asyncio
async def test_clipboard_copy_default_mime_type():
    """clipboard_copy defaults to text/plain and uses write_clipboard."""
    with patch("mcp_clipboard.server.write_clipboard", new_callable=AsyncMock) as mock:
        result = await clipboard_copy("hello")

    mock.assert_called_once_with("hello")
    assert "text/plain" in result


@pytest.mark.asyncio
async def test_clipboard_copy_rejects_binary_mime():
    """clipboard_copy rejects binary MIME types."""
    result = await clipboard_copy("data", mime_type="image/png")
    assert "Cannot write binary" in result


@pytest.mark.asyncio
async def test_clipboard_copy_rejects_audio_mime():
    """clipboard_copy rejects audio/* MIME types."""
    result = await clipboard_copy("data", mime_type="audio/mp3")
    assert "Cannot write binary" in result


@pytest.mark.asyncio
async def test_clipboard_copy_typed_error():
    """clipboard_copy surfaces ClipboardError from write_clipboard_typed."""
    with patch(
        "mcp_clipboard.server.write_clipboard_typed", side_effect=ClipboardError("unsupported")
    ):
        result = await clipboard_copy("<b>hi</b>", mime_type="text/html")

    assert "Error" in result
    assert "unsupported" in result


# ---------------------------------------------------------------------------
# Image write — backend writers (#108)
# ---------------------------------------------------------------------------


# Smallest possible PNG (1x1, fully transparent) — IHDR + IDAT + IEND.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a"  # PNG magic
    "0000000d49484452"  # IHDR length + chunk
    "0000000100000001080600000015b9da38"  # 1x1 RGBA
    "0000000a49444154789c63000100000005000182dd8a73"  # IDAT
    "0000000049454e44ae426082"  # IEND
)
# Smallest valid JPEG: SOI + APP0 minimal JFIF + SOF0 1x1 + SOS + EOI
_TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000"  # SOI + JFIF APP0
    "ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d"
    "1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d3832"
    "3c2e333432"  # DQT
    "ffc0000b08000100010101110000"  # SOF0 1x1
    "ffc4001f0000010501010101010100000000000000000102030405060708090a0b"  # DHT
    "ffda0008010100003f00d2cf20ffd9"  # SOS + EOI
)


@pytest.mark.asyncio
async def test_wayland_write_image_passes_mime_and_bytes():
    """_wayland_write_image invokes wl-copy --type <mime> with bytes on stdin."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _wayland_write_image(_TINY_PNG, "image/png")

    cmd = mock.call_args[0][0]
    assert cmd[0] == "wl-copy"
    assert "--type" in cmd
    assert "image/png" in cmd
    assert mock.call_args[0][1] == _TINY_PNG


@pytest.mark.asyncio
async def test_wayland_write_image_jpeg():
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _wayland_write_image(_TINY_JPEG, "image/jpeg")

    cmd = mock.call_args[0][0]
    assert "image/jpeg" in cmd
    assert mock.call_args[0][1] == _TINY_JPEG


@pytest.mark.asyncio
async def test_x11_write_image_passes_target_and_bytes():
    """_x11_write_image invokes xclip -selection clipboard -target <mime> -i."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _x11_write_image(_TINY_PNG, "image/png")

    cmd = mock.call_args[0][0]
    assert cmd[0] == "xclip"
    assert "-selection" in cmd and "clipboard" in cmd
    assert "-target" in cmd and "image/png" in cmd
    assert mock.call_args[0][1] == _TINY_PNG


@pytest.mark.asyncio
async def test_macos_write_image_uses_correct_uti():
    """_macos_write_image pipes osascript via stdin with the matching UTI."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_image(_TINY_PNG, "image/png")

    cmd = mock.call_args[0][0]
    assert cmd == ["osascript", "-"]
    script = mock.call_args[0][1].decode("utf-8")
    assert "public.png" in script
    assert "NSPasteboard" in script
    assert "setData" in script


@pytest.mark.asyncio
async def test_macos_write_image_jpeg_uti():
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_image(_TINY_JPEG, "image/jpeg")

    script = mock.call_args[0][1].decode("utf-8")
    assert "public.jpeg" in script


@pytest.mark.asyncio
async def test_macos_write_image_unsupported_mime():
    with pytest.raises(ClipboardError, match="macOS"):
        await _macos_write_image(b"\x00", "image/gif")


@pytest.mark.asyncio
async def test_macos_write_image_chunks_large_base64():
    """A large image must split base64 across multiple AppleScript lines.

    Mirrors _macos_write_typed's per-line-limit handling. The 32,767-char
    AppleScript parser limit applies even to scripts piped over stdin.
    """
    big_png = _TINY_PNG + (b"\x00" * 100_000)
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_image(big_png, "image/png")

    script = mock.call_args[0][1].decode("utf-8")
    for line in script.split("\n"):
        assert len(line) < 32_767, f"AppleScript line exceeds limit: {len(line)} chars"
    assert "set b64 to b64 &" in script  # chunking happened


@pytest.mark.asyncio
async def test_macos_write_image_empty_payload_chunk_fallback():
    """Empty image bytes must still produce syntactically valid AppleScript.

    `_validate_image_magic` upstream rejects empty bytes, but the backend
    function itself defensively handles the empty-chunks case (mirroring
    `_macos_write_typed`) so the AppleScript stays valid even if called
    directly with `b""`.
    """
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_image(b"", "image/png")

    script = mock.call_args[0][1].decode("utf-8")
    assert 'set b64 to ""' in script
    assert "public.png" in script


@pytest.mark.asyncio
async def test_macos_write_image_command_line_bounded_for_large_payloads():
    """Regression for #113: macOS osascript -e <script> would blow ARG_MAX
    (~1 MiB) for any image above ~750 KB after base64 framing — typical
    phone photos are 1-5 MB JPEG, so this triggered in practice. After the
    fix, the script flows over stdin via `osascript -`."""
    big_png = _TINY_PNG + (b"\x00" * (1024 * 1024))  # 1 MB+
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_image(big_png, "image/png")

    cmd = mock.call_args[0][0]
    cmdline_len = sum(len(arg) for arg in cmd) + max(0, len(cmd) - 1)
    assert cmdline_len < 1_048_576, (
        f"macOS osascript argv is {cmdline_len} chars; default ARG_MAX is "
        f"~1 MiB. Large images must use stdin via `osascript -`."
    )
    assert cmd == ["osascript", "-"]
    # The base64-framed script (over stdin) must scale with the payload.
    assert len(mock.call_args[0][1]) > 1_000_000


@pytest.mark.asyncio
async def test_windows_write_image_invokes_setimage():
    """_windows_write_image invokes PowerShell SetImage with a base64 payload."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _windows_write_image(_TINY_PNG, "image/png")

    script = mock.call_args[0][0][-1]
    assert "System.Windows.Forms.Clipboard" in script
    assert "SetImage" in script
    assert "FromBase64String" in script
    # The base64 payload flows over stdin, NOT in the script body.
    assert "[Console]::In.ReadToEnd()" in script


@pytest.mark.asyncio
async def test_windows_write_image_pipes_b64_via_stdin():
    """The base64 payload must be passed as stdin bytes, not interpolated."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _windows_write_image(_TINY_PNG, "image/png")

    stdin_bytes = mock.call_args[0][1]
    # stdin bytes must be base64-decodable and decode back to the original.
    import base64 as _b64

    assert _b64.b64decode(stdin_bytes.decode("ascii")) == _TINY_PNG


@pytest.mark.asyncio
async def test_windows_write_image_command_line_bounded_for_large_payloads():
    """Regression for the QA Round 2 F1 finding: Windows CreateProcess caps
    lpCommandLine at 32,767 chars. The previous implementation interpolated
    the base64 inline, which blew the cap for any image >~24 KB raw. The
    fixed implementation pipes via stdin, so the constructed argv stays a
    fixed ~400 chars regardless of payload size.
    """
    big_png = _TINY_PNG + (b"\x00" * (1024 * 1024))  # 1 MB+
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _windows_write_image(big_png, "image/png")

    cmd = mock.call_args[0][0]
    cmdline_len = sum(len(arg) for arg in cmd) + max(0, len(cmd) - 1)  # +spaces
    assert cmdline_len < 32_767, (
        f"PowerShell command line is {cmdline_len} chars; CreateProcess caps "
        f"lpCommandLine at 32,767. Inputs of any size must use stdin."
    )
    # Bytes must flow over stdin proportional to the input.
    stdin_bytes = mock.call_args[0][1]
    assert len(stdin_bytes) > 1_000_000


@pytest.mark.asyncio
async def test_windows_write_image_unsupported_mime():
    with pytest.raises(ClipboardError, match="Windows"):
        await _windows_write_image(b"\x00", "image/webp")


# ---------------------------------------------------------------------------
# Image write — public API (write_clipboard_image)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_clipboard_image_dispatches():
    """write_clipboard_image dispatches to the right backend."""
    mock_writer = AsyncMock()
    with (
        patch("mcp_clipboard.clipboard._get_backend", return_value="x11"),
        patch.dict("mcp_clipboard.clipboard._IMAGE_WRITERS", {"x11": mock_writer}),
    ):
        await write_clipboard_image(_TINY_PNG, "image/png")

    mock_writer.assert_called_once_with(_TINY_PNG, "image/png")


@pytest.mark.asyncio
async def test_write_clipboard_image_rejects_unsupported_mime():
    with pytest.raises(ClipboardError, match="Unsupported"):
        await write_clipboard_image(_TINY_PNG, "image/gif")


@pytest.mark.asyncio
async def test_write_clipboard_image_rejects_oversized():
    """Image larger than the cap raises ClipboardSizeError before the backend runs."""
    huge = _TINY_PNG + (b"\x00" * (15 * 1024 * 1024))
    mock_writer = AsyncMock()
    with (
        patch("mcp_clipboard.clipboard._get_backend", return_value="x11"),
        patch.dict("mcp_clipboard.clipboard._IMAGE_WRITERS", {"x11": mock_writer}),
    ):
        with pytest.raises(ClipboardSizeError):
            await write_clipboard_image(huge, "image/png")
    mock_writer.assert_not_called()


@pytest.mark.asyncio
async def test_write_clipboard_image_rejects_png_with_jpeg_mime():
    """Magic byte mismatch must be caught before any subprocess runs."""
    mock_writer = AsyncMock()
    with (
        patch("mcp_clipboard.clipboard._get_backend", return_value="x11"),
        patch.dict("mcp_clipboard.clipboard._IMAGE_WRITERS", {"x11": mock_writer}),
    ):
        with pytest.raises(ClipboardError, match="JPEG header"):
            await write_clipboard_image(_TINY_PNG, "image/jpeg")
    mock_writer.assert_not_called()


@pytest.mark.asyncio
async def test_write_clipboard_image_rejects_jpeg_with_png_mime():
    mock_writer = AsyncMock()
    with (
        patch("mcp_clipboard.clipboard._get_backend", return_value="x11"),
        patch.dict("mcp_clipboard.clipboard._IMAGE_WRITERS", {"x11": mock_writer}),
    ):
        with pytest.raises(ClipboardError, match="PNG header"):
            await write_clipboard_image(_TINY_JPEG, "image/png")
    mock_writer.assert_not_called()


@pytest.mark.asyncio
async def test_write_clipboard_image_rejects_garbage():
    """Random bytes claiming to be PNG must be rejected on the magic check."""
    with pytest.raises(ClipboardError, match="PNG header"):
        await write_clipboard_image(b"not an image at all", "image/png")


# ---------------------------------------------------------------------------
# Image write — clipboard_copy_image tool
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


@pytest.mark.asyncio
async def test_clipboard_copy_image_success_png():
    with patch("mcp_clipboard.server.write_clipboard_image", new_callable=AsyncMock) as mock:
        result = await clipboard_copy_image(_b64(_TINY_PNG), "image/png")

    mock.assert_called_once_with(_TINY_PNG, "image/png")
    assert "image/png" in result
    assert "Copied" in result


@pytest.mark.asyncio
async def test_clipboard_copy_image_success_jpeg():
    with patch("mcp_clipboard.server.write_clipboard_image", new_callable=AsyncMock) as mock:
        result = await clipboard_copy_image(_b64(_TINY_JPEG), "image/jpeg")

    mock.assert_called_once_with(_TINY_JPEG, "image/jpeg")
    assert "image/jpeg" in result


@pytest.mark.asyncio
async def test_clipboard_copy_image_default_mime_is_png():
    with patch("mcp_clipboard.server.write_clipboard_image", new_callable=AsyncMock) as mock:
        await clipboard_copy_image(_b64(_TINY_PNG))

    assert mock.call_args[0][1] == "image/png"


@pytest.mark.asyncio
async def test_clipboard_copy_image_normalizes_mime_case():
    with patch("mcp_clipboard.server.write_clipboard_image", new_callable=AsyncMock) as mock:
        await clipboard_copy_image(_b64(_TINY_PNG), "  IMAGE/PNG  ")

    assert mock.call_args[0][1] == "image/png"


@pytest.mark.asyncio
async def test_clipboard_copy_image_rejects_unsupported_mime():
    result = await clipboard_copy_image(_b64(_TINY_PNG), "image/gif")
    assert "Unsupported" in result
    assert "image/gif" in result


@pytest.mark.asyncio
async def test_clipboard_copy_image_rejects_invalid_base64():
    result = await clipboard_copy_image("not!base64!", "image/png")
    assert "valid base64" in result


@pytest.mark.asyncio
async def test_clipboard_copy_image_surfaces_clipboard_error():
    with patch(
        "mcp_clipboard.server.write_clipboard_image",
        side_effect=ClipboardError("backend exploded"),
    ):
        result = await clipboard_copy_image(_b64(_TINY_PNG), "image/png")
    assert "Error writing image" in result
    assert "backend exploded" in result


@pytest.mark.asyncio
async def test_clipboard_copy_image_surfaces_size_error():
    with patch(
        "mcp_clipboard.server.write_clipboard_image",
        side_effect=ClipboardSizeError("too big"),
    ):
        result = await clipboard_copy_image(_b64(_TINY_PNG), "image/png")
    assert "Error writing image" in result
    assert "too big" in result


@pytest.mark.asyncio
async def test_clipboard_copy_image_surfaces_magic_mismatch():
    """A host model passing PNG bytes with mime_type='image/jpeg' should
    get a clean error from the tool, not a stack trace."""
    result = await clipboard_copy_image(_b64(_TINY_PNG), "image/jpeg")
    assert "Error writing image" in result
    assert "JPEG header" in result


# ---------------------------------------------------------------------------
# Markdown -> rich-text write — clipboard_copy_markdown (#109)
# ---------------------------------------------------------------------------


_SAMPLE_MARKDOWN = "# Title\n\n- one\n- two\n\n**bold** and `code`."


def test_pick_single_mime_prefers_html():
    chosen = _pick_single_mime({"text/plain": "p", "text/html": "h"})
    assert chosen == ("text/html", "h")


def test_pick_single_mime_falls_back_to_plain():
    chosen = _pick_single_mime({"text/plain": "p"})
    assert chosen == ("text/plain", "p")


def test_pick_single_mime_unknown_falls_back_to_first_key():
    """If neither html nor plain is present, return the first dict entry."""
    chosen = _pick_single_mime({"text/csv": "c", "application/foo": "f"})
    assert chosen == ("text/csv", "c")


def test_pick_single_mime_empty_returns_none():
    assert _pick_single_mime({}) is None


@pytest.mark.asyncio
async def test_wayland_write_multi_picks_html():
    """Wayland is single-MIME-per-call; multi-format dispatch picks text/html."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _wayland_write_multi({"text/html": "<p>hi</p>", "text/plain": "hi"})

    cmd = mock.call_args[0][0]
    assert cmd[0] == "wl-copy"
    assert "--type" in cmd and "text/html" in cmd
    assert "text/plain" not in cmd  # plain dropped
    assert mock.call_args[0][1] == b"<p>hi</p>"


@pytest.mark.asyncio
async def test_wayland_write_multi_falls_back_to_plain():
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _wayland_write_multi({"text/plain": "just text"})

    cmd = mock.call_args[0][0]
    assert "text/plain" in cmd
    assert mock.call_args[0][1] == b"just text"


@pytest.mark.asyncio
async def test_wayland_write_multi_empty_is_noop():
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _wayland_write_multi({})
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_x11_write_multi_picks_html():
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _x11_write_multi({"text/html": "<p>hi</p>", "text/plain": "hi"})

    cmd = mock.call_args[0][0]
    assert cmd[0] == "xclip"
    assert "-target" in cmd and "text/html" in cmd
    assert mock.call_args[0][1] == b"<p>hi</p>"


@pytest.mark.asyncio
async def test_x11_write_multi_empty_is_noop():
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _x11_write_multi({})
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_macos_write_multi_one_clear_n_setdata():
    """The macOS multi-format script must call clearContents exactly once and
    setData:forType: once per format. Atomic write semantics depend on it."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_multi({"text/html": "<p>hi</p>", "text/plain": "hi"})

    cmd = mock.call_args[0][0]
    assert cmd == ["osascript", "-"]
    script = mock.call_args[0][1].decode("utf-8")
    assert script.count("pb's clearContents()") == 1
    assert script.count("setData:") == 2
    assert "public.html" in script
    assert "public.utf8-plain-text" in script


@pytest.mark.asyncio
async def test_macos_write_multi_drops_unknown_mimes():
    """Unsupported MIMEs are skipped silently — multi-format is best-effort."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_multi({"text/html": "<p>hi</p>", "application/foo": "x"})

    script = mock.call_args[0][1].decode("utf-8")
    assert script.count("setData:") == 1
    assert "public.html" in script
    assert "application/foo" not in script


@pytest.mark.asyncio
async def test_macos_write_multi_empty_after_filter_is_noop():
    """If no recognized MIMEs survive filtering, no subprocess is invoked."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_multi({"application/octet-stream": "x"})
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_macos_write_multi_chunks_large_content_per_format():
    """Each format's base64 must respect the 32,767-char per-line AppleScript
    parser limit, even though the total script is now stdin-piped."""
    big_html = "<p>" + ("X" * 100_000) + "</p>"
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_multi({"text/html": big_html, "text/plain": "small"})

    script = mock.call_args[0][1].decode("utf-8")
    for line in script.split("\n"):
        assert len(line) < 32_767, f"AppleScript line exceeds limit: {len(line)} chars"
    # Chunking must have happened for the large format.
    assert "set b64_0 to b64_0 &" in script


@pytest.mark.asyncio
async def test_windows_write_multi_filters_and_forwards_to_win32_wrapper():
    """_windows_write_multi forwards recognized MIMEs to clipboard_win32.write_multi,
    wrapping text/html in CF_HTML before handoff and dropping unsupported
    MIMEs silently. The Win32 wrapper performs the OpenClipboard ->
    EmptyClipboard -> SetClipboardData per format -> CloseClipboard
    transaction atomically."""
    with patch("mcp_clipboard.clipboard_win32.write_multi") as mock_multi:
        await _windows_write_multi(
            {
                "text/html": "<p>hi</p>",
                "text/plain": "hi",
                "text/rtf": r"{\rtf1 hi}",
                "application/foo": "dropped",
            }
        )

    mock_multi.assert_called_once()
    payloads = mock_multi.call_args[0][0]
    # Recognized MIMEs preserved; unsupported dropped.
    assert set(payloads.keys()) == {"text/html", "text/plain", "text/rtf"}
    # text/html wrapped in CF_HTML; other MIMEs unchanged.
    assert payloads["text/plain"] == "hi"
    assert payloads["text/rtf"] == r"{\rtf1 hi}"
    html = payloads["text/html"]
    assert html.startswith("Version:0.9")
    assert "StartHTML:" in html
    assert "<!--StartFragment-->" in html
    assert "<p>hi</p>" in html


@pytest.mark.asyncio
async def test_windows_write_multi_empty_after_filter_is_noop():
    """If every input MIME is unsupported, the wrapper is never called."""
    with patch("mcp_clipboard.clipboard_win32.write_multi") as mock_multi:
        await _windows_write_multi({"application/foo": "x"})
    mock_multi.assert_not_called()


@pytest.mark.asyncio
async def test_windows_write_multi_wraps_win32_errors_as_clipboard_error():
    """Errors from the win32 wrapper surface as ClipboardError uniformly."""
    with patch(
        "mcp_clipboard.clipboard_win32.write_multi",
        side_effect=RuntimeError("OpenClipboard failed"),
    ):
        with pytest.raises(ClipboardError, match="Windows clipboard multi-format write failed"):
            await _windows_write_multi({"text/plain": "hi"})


@pytest.mark.asyncio
async def test_macos_write_multi_empty_content_per_format_chunk_fallback():
    """Even if a recognized MIME has empty content, the script builder must
    produce syntactically valid AppleScript via the [""] chunks fallback —
    matches the defensive parity with _macos_pasteboard_script."""
    with patch("mcp_clipboard.clipboard._run_with_stdin", new_callable=AsyncMock) as mock:
        await _macos_write_multi({"text/html": "", "text/plain": "hi"})

    script = mock.call_args[0][1].decode("utf-8")
    # The empty html still produces a valid `set b64_0 to ""` line.
    assert 'set b64_0 to ""' in script
    # The non-empty plain still chunks normally.
    assert "public.utf8-plain-text" in script


@pytest.mark.asyncio
async def test_write_clipboard_multi_format_dispatches():
    mock_writer = AsyncMock()
    with (
        patch("mcp_clipboard.clipboard._get_backend", return_value="x11"),
        patch.dict("mcp_clipboard.clipboard._MULTI_WRITERS", {"x11": mock_writer}),
    ):
        await write_clipboard_multi_format({"text/html": "<p>hi</p>"})

    mock_writer.assert_called_once_with({"text/html": "<p>hi</p>"})


# --- clipboard_copy_markdown tool surface ---


@pytest.mark.asyncio
async def test_clipboard_copy_markdown_renders_and_writes_both_formats():
    """The tool renders markdown -> HTML and writes both formats via
    write_clipboard_multi_format."""
    with patch("mcp_clipboard.server.write_clipboard_multi_format", new_callable=AsyncMock) as mock:
        result = await clipboard_copy_markdown(_SAMPLE_MARKDOWN)

    formats = mock.call_args[0][0]
    assert set(formats) == {"text/html", "text/plain"}
    # Plain is the markdown source verbatim.
    assert formats["text/plain"] == _SAMPLE_MARKDOWN
    # HTML contains the rendered structure.
    assert "<h1>Title</h1>" in formats["text/html"]
    assert "<ul>" in formats["text/html"]
    assert "<strong>bold</strong>" in formats["text/html"]
    assert "<code>code</code>" in formats["text/html"]
    # Confirmation surfaces both byte counts.
    assert "Copied" in result
    assert "markdown" in result.lower()


@pytest.mark.asyncio
async def test_clipboard_copy_markdown_escapes_raw_html_by_default():
    """Raw HTML in the markdown source must be escaped, not passed through.
    This is the safe-by-construction posture: a model wanting hand-crafted
    HTML must opt in via clipboard_copy(mime_type='text/html')."""
    with patch("mcp_clipboard.server.write_clipboard_multi_format", new_callable=AsyncMock) as mock:
        await clipboard_copy_markdown("<script>alert(1)</script>\n\n# Title")

    html = mock.call_args[0][0]["text/html"]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<h1>Title</h1>" in html


@pytest.mark.asyncio
async def test_clipboard_copy_markdown_empty_string_handles_cleanly():
    with patch("mcp_clipboard.server.write_clipboard_multi_format", new_callable=AsyncMock) as mock:
        result = await clipboard_copy_markdown("")

    formats = mock.call_args[0][0]
    assert formats["text/plain"] == ""
    assert formats["text/html"] == ""
    assert "Copied" in result


@pytest.mark.asyncio
async def test_clipboard_copy_markdown_rejects_oversized():
    """Oversized markdown source is rejected before render or write."""
    huge = "x" * (3 * 1024 * 1024)  # 3 MB; default cap is 1 MiB
    with patch("mcp_clipboard.server.write_clipboard_multi_format", new_callable=AsyncMock) as mock:
        result = await clipboard_copy_markdown(huge)

    assert "exceeds clipboard write limit" in result
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_clipboard_copy_markdown_surfaces_clipboard_error():
    with patch(
        "mcp_clipboard.server.write_clipboard_multi_format",
        side_effect=ClipboardError("backend exploded"),
    ):
        result = await clipboard_copy_markdown(_SAMPLE_MARKDOWN)
    assert "Error writing to clipboard" in result
    assert "backend exploded" in result


@pytest.mark.asyncio
async def test_clipboard_copy_markdown_surfaces_render_error():
    """If markdown-it raises during render, the tool returns a clean error
    rather than letting the exception propagate up to the MCP transport."""
    with patch(
        "mcp_clipboard.server._render_markdown_to_html",
        side_effect=RuntimeError("parser exploded"),
    ):
        result = await clipboard_copy_markdown(_SAMPLE_MARKDOWN)
    assert "Failed to render markdown" in result
    assert "parser exploded" in result


# ---------------------------------------------------------------------------
# PRIMARY selection support (#110)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wayland_read_passes_primary_flag():
    """_wayland_read appends --primary when selection='primary'."""
    from mcp_clipboard.clipboard import _wayland_read

    with patch("mcp_clipboard.clipboard._run", new_callable=AsyncMock) as mock:
        mock.return_value = "selected text"
        await _wayland_read("text/plain", "primary")

    cmd = mock.call_args[0][0]
    assert "--primary" in cmd
    assert "wl-paste" in cmd[0]


@pytest.mark.asyncio
async def test_wayland_read_default_clipboard_omits_primary_flag():
    from mcp_clipboard.clipboard import _wayland_read

    with patch("mcp_clipboard.clipboard._run", new_callable=AsyncMock) as mock:
        mock.return_value = ""
        await _wayland_read("text/plain")

    cmd = mock.call_args[0][0]
    assert "--primary" not in cmd


@pytest.mark.asyncio
async def test_wayland_list_formats_primary():
    from mcp_clipboard.clipboard import _wayland_list_formats

    with patch("mcp_clipboard.clipboard._run", new_callable=AsyncMock) as mock:
        mock.return_value = "text/plain\n"
        await _wayland_list_formats("primary")

    cmd = mock.call_args[0][0]
    assert "--primary" in cmd
    assert "--list-types" in cmd


@pytest.mark.asyncio
async def test_wayland_read_image_primary():
    from mcp_clipboard.clipboard import _wayland_read_image

    with patch("mcp_clipboard.clipboard._run_binary", new_callable=AsyncMock) as mock:
        mock.return_value = b""
        await _wayland_read_image("image/png", "primary")

    cmd = mock.call_args[0][0]
    assert "--primary" in cmd


@pytest.mark.asyncio
async def test_x11_read_uses_primary_selection_arg():
    """_x11_read passes -selection primary when selection='primary'."""
    from mcp_clipboard.clipboard import _x11_read

    with patch("mcp_clipboard.clipboard._run", new_callable=AsyncMock) as mock:
        mock.return_value = ""
        await _x11_read("text/plain", "primary")

    cmd = mock.call_args[0][0]
    assert cmd[0] == "xclip"
    # `-selection primary` must be the consecutive args.
    sel_idx = cmd.index("-selection")
    assert cmd[sel_idx + 1] == "primary"


@pytest.mark.asyncio
async def test_x11_read_default_uses_clipboard_selection():
    from mcp_clipboard.clipboard import _x11_read

    with patch("mcp_clipboard.clipboard._run", new_callable=AsyncMock) as mock:
        mock.return_value = ""
        await _x11_read("text/plain")

    cmd = mock.call_args[0][0]
    sel_idx = cmd.index("-selection")
    assert cmd[sel_idx + 1] == "clipboard"


@pytest.mark.asyncio
async def test_x11_list_formats_primary():
    from mcp_clipboard.clipboard import _x11_list_formats

    with patch("mcp_clipboard.clipboard._run", new_callable=AsyncMock) as mock:
        mock.return_value = ""
        await _x11_list_formats("primary")

    cmd = mock.call_args[0][0]
    sel_idx = cmd.index("-selection")
    assert cmd[sel_idx + 1] == "primary"


@pytest.mark.asyncio
async def test_x11_read_image_primary():
    from mcp_clipboard.clipboard import _x11_read_image

    with patch("mcp_clipboard.clipboard._run_binary", new_callable=AsyncMock) as mock:
        mock.return_value = b""
        await _x11_read_image("image/png", "primary")

    cmd = mock.call_args[0][0]
    sel_idx = cmd.index("-selection")
    assert cmd[sel_idx + 1] == "primary"


@pytest.mark.asyncio
async def test_macos_read_rejects_primary():
    """macOS has no PRIMARY analog; non-default selection raises ClipboardError."""
    from mcp_clipboard.clipboard import _macos_read

    with pytest.raises(ClipboardError, match="macOS does not support"):
        await _macos_read("text/plain", "primary")


@pytest.mark.asyncio
async def test_windows_read_rejects_primary():
    from mcp_clipboard.clipboard import _windows_read

    with pytest.raises(ClipboardError, match="Windows does not support"):
        await _windows_read("text/plain", "primary")


@pytest.mark.asyncio
async def test_macos_list_formats_rejects_primary():
    from mcp_clipboard.clipboard import _macos_list_formats

    with pytest.raises(ClipboardError, match="macOS"):
        await _macos_list_formats("primary")


@pytest.mark.asyncio
async def test_windows_list_formats_rejects_primary():
    from mcp_clipboard.clipboard import _windows_list_formats

    with pytest.raises(ClipboardError, match="Windows"):
        await _windows_list_formats("primary")


@pytest.mark.asyncio
async def test_macos_read_image_rejects_primary():
    from mcp_clipboard.clipboard import _macos_read_image

    with pytest.raises(ClipboardError, match="macOS"):
        await _macos_read_image("image/png", "primary")


@pytest.mark.asyncio
async def test_windows_read_image_rejects_primary():
    from mcp_clipboard.clipboard import _windows_read_image

    with pytest.raises(ClipboardError, match="Windows"):
        await _windows_read_image("image/png", "primary")


@pytest.mark.asyncio
async def test_invalid_selection_raises_at_backend_layer():
    """Backend selection validation rejects unknown values cleanly on Wayland."""
    from mcp_clipboard.clipboard import _wayland_read

    with pytest.raises(ClipboardError, match="Invalid selection"):
        await _wayland_read("text/plain", "secondary")


# --- Public API threading ---


@pytest.mark.asyncio
async def test_read_clipboard_passes_selection_through():
    mock_reader = AsyncMock(return_value="primary text")
    with (
        patch("mcp_clipboard.clipboard._get_backend", return_value="wayland"),
        patch.dict("mcp_clipboard.clipboard._READERS", {"wayland": mock_reader}),
    ):
        result = await read_clipboard("text/plain", "primary")

    assert result == "primary text"
    mock_reader.assert_called_once_with("text/plain", "primary")


@pytest.mark.asyncio
async def test_list_clipboard_formats_passes_selection_through():
    mock_lister = AsyncMock(return_value=["text/plain"])
    with (
        patch("mcp_clipboard.clipboard._get_backend", return_value="x11"),
        patch.dict("mcp_clipboard.clipboard._FORMAT_LISTERS", {"x11": mock_lister}),
    ):
        await list_clipboard_formats("primary")

    mock_lister.assert_called_once_with("primary")


@pytest.mark.asyncio
async def test_read_clipboard_image_passes_selection_through():
    mock_reader = AsyncMock(return_value=b"")
    with (
        patch("mcp_clipboard.clipboard._get_backend", return_value="x11"),
        patch.dict("mcp_clipboard.clipboard._IMAGE_READERS", {"x11": mock_reader}),
        patch.dict(
            "mcp_clipboard.clipboard._FORMAT_LISTERS",
            {"x11": AsyncMock(return_value=[])},
        ),
    ):
        await read_clipboard_image("image/png", "primary")

    mock_reader.assert_called_once_with("image/png", "primary")


# --- Tool surface ---


@pytest.mark.asyncio
async def test_clipboard_paste_accepts_selection_primary():
    """clipboard_paste(selection='primary') routes the read through with primary."""
    with patch("mcp_clipboard.server.read_clipboard", new=_mock_read(text="hello primary")):
        result = await clipboard_paste(selection="primary")
    assert "hello primary" in result


@pytest.mark.asyncio
async def test_clipboard_paste_threads_selection_to_read_clipboard():
    """clipboard_paste must pass selection to its read_clipboard calls so the
    correct buffer is sourced."""
    seen: list[tuple[str, str]] = []

    async def spy(mime_type: str = "text/plain", selection: str = "clipboard") -> str:
        seen.append((mime_type, selection))
        return ""

    with patch("mcp_clipboard.server.read_clipboard", new=spy):
        await clipboard_paste(selection="primary")

    assert all(s == "primary" for _, s in seen)
    assert any(m == "text/html" for m, _ in seen)
    assert any(m == "text/plain" for m, _ in seen)


@pytest.mark.asyncio
async def test_clipboard_paste_rejects_invalid_selection():
    result = await clipboard_paste(selection="secondary")
    assert "Invalid selection" in result


@pytest.mark.asyncio
async def test_clipboard_read_raw_accepts_primary():
    with patch("mcp_clipboard.server.read_clipboard", return_value="from primary") as mock:
        result = await clipboard_read_raw(mime_type="text/plain", selection="primary")

    assert "from primary" in result
    mock.assert_called_once_with("text/plain", "primary")


@pytest.mark.asyncio
async def test_clipboard_read_raw_rejects_invalid_selection():
    result = await clipboard_read_raw(mime_type="text/plain", selection="bogus")
    assert "Invalid selection" in result


@pytest.mark.asyncio
async def test_clipboard_list_formats_accepts_primary():
    with patch("mcp_clipboard.server.list_clipboard_formats", return_value=["text/plain"]) as mock:
        result = await clipboard_list_formats(selection="primary")

    assert "text/plain" in result
    mock.assert_called_once_with("primary")


@pytest.mark.asyncio
async def test_clipboard_list_formats_rejects_invalid_selection():
    result = await clipboard_list_formats(selection="bogus")
    assert "Invalid selection" in result


@pytest.mark.asyncio
async def test_clipboard_paste_normalizes_selection_case():
    with patch("mcp_clipboard.server.read_clipboard", new=_mock_read(text="x")):
        result = await clipboard_paste(selection=" PRIMARY ")
    # Normalized to lowercase; would have been rejected as "Invalid" otherwise.
    assert "Invalid selection" not in result


# ---------------------------------------------------------------------------
# __version__ resilience (#28)
# ---------------------------------------------------------------------------


def test_version_fallback_when_not_installed():
    """__version__ should not crash when the package is not installed."""
    from importlib.metadata import PackageNotFoundError
    from unittest.mock import patch as mock_patch

    with mock_patch(
        "importlib.metadata.version", side_effect=PackageNotFoundError("mcp-clipboard")
    ):
        # Re-import to trigger the version lookup
        import importlib

        import mcp_clipboard

        importlib.reload(mcp_clipboard)
        assert mcp_clipboard.__version__ == "0.0.0+dev"

    # Restore the real version
    importlib.reload(mcp_clipboard)


# ---------------------------------------------------------------------------
# CLI flags: --version / --help / --check (#130)
# ---------------------------------------------------------------------------


def test_cli_version_prints_and_exits(capsys, monkeypatch):
    """`mcp-clipboard --version` prints version and returns without starting the server."""
    from mcp_clipboard.server import main

    monkeypatch.setattr("sys.argv", ["mcp-clipboard", "--version"])
    with patch("mcp_clipboard.server.mcp.run") as mock_run:
        main()

    mock_run.assert_not_called()
    out = capsys.readouterr().out
    assert "mcp-clipboard" in out
    # Version string should be present (any non-empty version is fine).
    assert any(c.isdigit() for c in out), f"No version number in output: {out!r}"


def test_cli_help_prints_and_exits(capsys, monkeypatch):
    """`mcp-clipboard --help` prints help and returns without starting the server."""
    from mcp_clipboard.server import main

    monkeypatch.setattr("sys.argv", ["mcp-clipboard", "--help"])
    with patch("mcp_clipboard.server.mcp.run") as mock_run:
        main()

    mock_run.assert_not_called()
    out = capsys.readouterr().out
    assert "Usage" in out
    assert "--check" in out
    assert "https://github.com/cmeans/mcp-clipboard" in out


def test_cli_check_success_returns_0(capsys, monkeypatch):
    """--check exits 0 when the platform backend is reachable."""
    from mcp_clipboard.server import _run_check

    with patch("mcp_clipboard.server._detect_backend", return_value="wayland"):
        rc = _run_check()

    assert rc == 0
    out = capsys.readouterr().out
    assert "wayland" in out
    assert "OK" in out


def test_cli_check_failure_returns_1(capsys, monkeypatch):
    """--check exits 1 with a diagnostic when no backend is reachable."""
    from mcp_clipboard.server import _run_check

    with patch(
        "mcp_clipboard.server._detect_backend",
        side_effect=ClipboardError("No clipboard tool found. Install wl-paste or xclip."),
    ):
        rc = _run_check()

    assert rc == 1
    captured = capsys.readouterr()
    assert "NOT AVAILABLE" in captured.err
    assert "wl-paste" in captured.err


def test_cli_check_dispatches_through_main(monkeypatch):
    """`mcp-clipboard --check` exits via sys.exit(rc) and never reaches mcp.run."""
    from mcp_clipboard.server import main

    monkeypatch.setattr("sys.argv", ["mcp-clipboard", "--check"])
    with (
        patch("mcp_clipboard.server._detect_backend", return_value="wayland"),
        patch("mcp_clipboard.server.mcp.run") as mock_run,
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    # _run_check returned 0; main() called sys.exit(0).
    assert exc_info.value.code == 0
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# clipboard_version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clipboard_version_returns_package_version():
    """clipboard_version returns the live package __version__."""
    from mcp_clipboard import __version__

    result = await clipboard_version()
    assert result == {"name": "mcp-clipboard", "version": __version__}


def test_load_instruction_clipboard_version():
    """The clipboard_version instruction file is shipped and loadable."""
    result = _load_instruction("clipboard_version")
    assert isinstance(result, str)
    assert len(result) > 0
    assert "version" in result.lower()
