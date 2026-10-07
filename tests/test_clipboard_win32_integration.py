"""Integration tests for the Win32 clipboard backend.

Round-trips text/plain (with non-ASCII codepoints), text/html, text/rtf, and
image/svg+xml through the real Windows clipboard via clipboard_win32. These
tests are the per-push CI replacement for the QEMU manual-test loop the old
PowerShell-subprocess backend forced.

Skip on non-Windows hosts. The sys_platform marker on pywin32 in pyproject.toml
means the import would also fail elsewhere, but we skip explicitly so a CI
runner without Windows skips the file cleanly rather than erroring at collect.

Marked @pytest.mark.integration so they only run when the runner has a real
clipboard available -- the GitHub Actions windows-latest job runs them; the
default `pytest` invocation does not.
"""

from __future__ import annotations

import sys

import pytest

if sys.platform != "win32":
    pytest.skip("clipboard_win32 only runs on Windows", allow_module_level=True)

# Imports happen after the skip so non-Windows pytest collection does not
# trigger the pywin32 import error.
from mcp_clipboard import clipboard_win32 as _w32

pytestmark = pytest.mark.integration


# --- Reset helper ----------------------------------------------------------


def _reset_clipboard() -> None:
    """Drop a known marker on the clipboard so a test starts from a defined
    state rather than inheriting whatever was there from a prior test or the
    user's actual clipboard contents."""
    _w32.write_text("__SUITE_RESET__", "text/plain")


# --- text/plain round-trip -------------------------------------------------


def test_plain_ascii_roundtrip():
    _reset_clipboard()
    _w32.write_text("Hello, world.", "text/plain")
    assert _w32.read_text("text/plain") == "Hello, world."


def test_plain_utf8_punctuation_roundtrip():
    """Em dash (U+2014), curly quotes, ellipsis -- the codepoints that the
    PowerShell-backed backend lossy-transliterated to ASCII via CP1252.
    Must round-trip byte-for-byte through CF_UNICODETEXT."""
    _reset_clipboard()
    # Built from explicit \N escapes so ruff's RUF001 ambiguity check
    # does not flag the curly quotes / em dash / ellipsis as look-alikes
    # of ASCII grave/quote/period.
    sample = (
        "before \N{EM DASH} \N{LEFT SINGLE QUOTATION MARK}curly"
        "\N{RIGHT SINGLE QUOTATION MARK} \N{LEFT DOUBLE QUOTATION MARK}"
        "double\N{RIGHT DOUBLE QUOTATION MARK} \N{HORIZONTAL ELLIPSIS}"
        " after"
    )
    _w32.write_text(sample, "text/plain")
    assert _w32.read_text("text/plain") == sample


def test_plain_cjk_arabic_emoji_roundtrip():
    """CJK + Arabic + emoji -- the codepoints CP1252 substituted with U+003F
    in the prior PowerShell-stdout-encoding bug class. CF_UNICODETEXT carries
    UTF-16 LE natively so all of these survive."""
    _reset_clipboard()
    sample = "こんにちは مرحبا \U0001f680"
    _w32.write_text(sample, "text/plain")
    assert _w32.read_text("text/plain") == sample


def test_plain_multiline_no_crlf_normalization():
    """The PowerShell Set-Clipboard backend appended CRLF to writes; the
    Win32 SetClipboardData(CF_UNICODETEXT, ...) path does not. This test
    documents the new (more-correct) behavior: bytes round-trip exactly
    as written, no implicit line-ending mutation."""
    _reset_clipboard()
    sample = "line1\nline2\nline3"
    _w32.write_text(sample, "text/plain")
    assert _w32.read_text("text/plain") == sample


# --- text/html round-trip --------------------------------------------------


def test_html_roundtrip_carries_cf_html_wrapper():
    """text/html is stored as the CF_HTML wrapper format -- Version + offsets
    + body. _windows_write_typed wraps the content via _windows_html_clipboard_wrap
    before handing to clipboard_win32. The read side returns the wrapper bytes
    decoded UTF-8; the parser layer in server.py is what extracts the body
    fragment from the offsets."""
    from mcp_clipboard.clipboard import _windows_html_clipboard_wrap

    _reset_clipboard()
    body = "<p>Hello <strong>world</strong></p>"
    wrapped = _windows_html_clipboard_wrap(body)
    _w32.write_text(wrapped, "text/html")
    out = _w32.read_text("text/html")
    assert "Version:0.9" in out
    assert "StartHTML:" in out
    assert "<!--StartFragment-->" in out
    assert body in out


# --- text/rtf round-trip ---------------------------------------------------


def test_rtf_roundtrip():
    _reset_clipboard()
    rtf = r"{\rtf1\ansi Hello, {\b world}!}"
    _w32.write_text(rtf, "text/rtf")
    assert _w32.read_text("text/rtf") == rtf


# --- image/svg+xml round-trip ----------------------------------------------


def test_svg_roundtrip_after_text_state():
    """SVG copy after a text-only clipboard -- the case the PowerShell
    backend silently no-opped on (mc-005, mc-020). With the single-process
    Win32 backend there is no cross-process propagation race to lose."""
    _reset_clipboard()
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<rect width="100" height="100" fill="green"/></svg>'
    )
    _w32.write_text(svg, "image/svg+xml")
    assert _w32.read_text("image/svg+xml") == svg


def test_svg_roundtrip_with_non_ascii_text_node():
    """SVG containing non-ASCII text in a <text> element. UTF-8 byte
    payload must survive the registered-format storage and read-back."""
    _reset_clipboard()
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="50">'
        '<text x="10" y="30">café — 日本</text></svg>'
    )
    _w32.write_text(svg, "image/svg+xml")
    out = _w32.read_text("image/svg+xml")
    assert out == svg
    # Specifically assert the non-ASCII codepoints are intact, not transliterated.
    assert "café" in out
    assert "—" in out
    assert "日本" in out


# --- multi-format atomic write ---------------------------------------------


def test_multi_format_writes_html_and_plain_atomically():
    """clipboard_copy_markdown's primary use case: HTML and plain text on
    the clipboard simultaneously so paste targets pick the format that
    suits them. Atomic via single OpenClipboard transaction in the Win32
    backend."""
    from mcp_clipboard.clipboard import _windows_html_clipboard_wrap

    _reset_clipboard()
    html_body = "<h1>Heading</h1>"
    wrapped = _windows_html_clipboard_wrap(html_body)
    _w32.write_multi({"text/html": wrapped, "text/plain": "Heading"})

    assert _w32.read_text("text/plain") == "Heading"
    out_html = _w32.read_text("text/html")
    assert html_body in out_html


# --- list_formats ----------------------------------------------------------


def test_list_formats_after_plain_write_includes_unicodetext():
    _reset_clipboard()
    _w32.write_text("hi", "text/plain")
    formats = _w32.list_formats()
    assert "UnicodeText" in formats


def test_list_formats_after_svg_write_includes_custom_format():
    _reset_clipboard()
    _w32.write_text("<svg/>", "image/svg+xml")
    formats = _w32.list_formats()
    assert "image/svg+xml" in formats


def test_list_formats_after_multi_write_includes_html_and_text():
    """The MCP-visible regression #143 reported (mc-005 / mc-020) was that
    the next list_formats call after a non-text/plain write did not see the
    new format. The single-process Win32 backend does not have that race."""
    from mcp_clipboard.clipboard import _windows_html_clipboard_wrap

    _reset_clipboard()
    _w32.write_multi(
        {
            "text/html": _windows_html_clipboard_wrap("<p>hi</p>"),
            "text/plain": "hi",
        }
    )
    formats = _w32.list_formats()
    assert "HTML Format" in formats
    assert "UnicodeText" in formats


# --- Read empty ------------------------------------------------------------


def test_read_text_returns_empty_for_absent_format():
    """If a format is not on the clipboard, read_text returns "" rather than
    raising. Lets server.py distinguish "not present" from "error" cleanly."""
    _reset_clipboard()  # sets text/plain only
    assert _w32.read_text("image/svg+xml") == ""
