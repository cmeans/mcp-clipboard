"""Platform-agnostic clipboard access with support for rich (HTML) and plain text formats.

Detection order:
  1. Wayland (wl-paste / wl-copy)
  2. X11 (xclip)
  3. macOS (osascript / pbpaste / pbcopy)
  4. Windows (PowerShell)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import platform
import shutil
import stat
from pathlib import Path

from . import clipboard_win32 as _w32

logger = logging.getLogger(__name__)


class ClipboardError(Exception):
    """Raised when clipboard access fails."""


class ClipboardSizeError(ClipboardError):
    """Raised when clipboard content exceeds a configured size cap."""


# AppleScript source has a 32,767-character per-line limit. Chunk size for
# base64 string literals must stay well under that to leave headroom for
# the surrounding `set b64 to "..."` syntax.
_APPLESCRIPT_CHUNK = 4000

# Cap on image read size. A large clipboard bitmap (e.g. 100 MB uncompressed
# TIFF screenshot) becomes ~133 MB base64 in a single MCP response and can
# time out or drop the MCP transport. The cap is a wire-level guard;
# backend memory is not bounded (the full image is still buffered before
# the size check fires). Configurable via env var.
_MAX_IMAGE_BYTES = int(os.environ.get("MCP_CLIPBOARD_MAX_IMAGE_BYTES", 10 * 1024 * 1024))


def base_mime_type(mime: str) -> str:
    """Strip parameters from a MIME type string.

    MIME types on the clipboard often include parameters after a semicolon
    (e.g., ``text/plain;charset=utf-8``).  This returns just the base type.
    """
    return mime.split(";", 1)[0].strip()


async def _run_subprocess(
    cmd: list[str],
    *,
    timeout: float = 5.0,
    env: dict[str, str] | None = None,
    allow_empty_exit: bool = True,
) -> bytes:
    """Run a subprocess and return its stdout as raw bytes.

    When *allow_empty_exit* is ``True``, exit code 1 is treated as "format not
    available" and returns empty bytes.  This is the expected behavior for
    ``wl-paste`` ("No suitable type of content") and ``xclip`` ("target not
    available").  Set it to ``False`` for macOS and Windows backends where exit
    code 1 indicates a real error (script failure, permission denied, etc.).
    """
    proc: asyncio.subprocess.Process | None = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as fnf:
            raise ClipboardError(f"Command not found: {cmd[0]}") from fnf
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as te:
            proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            raise ClipboardError(f"Clipboard command timed out: {' '.join(cmd)}") from te

        if proc.returncode != 0:
            if allow_empty_exit and proc.returncode == 1:
                return b""
            err = stderr.decode(errors="replace").strip()
            raise ClipboardError(f"Clipboard command failed (rc={proc.returncode}): {err}")

        return stdout
    finally:
        # Belt-and-suspenders cleanup for paths that bypass the explicit
        # kill above -- specifically asyncio.CancelledError (BaseException)
        # from a canceled MCP request, which would otherwise orphan the
        # subprocess. kill() is a no-op once the process has exited.
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()


async def _run(
    cmd: list[str],
    *,
    timeout: float = 5.0,
    env: dict[str, str] | None = None,
    allow_empty_exit: bool = True,
) -> str:
    """Run a subprocess and return its stdout as a string."""
    data = await _run_subprocess(cmd, timeout=timeout, env=env, allow_empty_exit=allow_empty_exit)
    return data.decode(errors="replace")


async def _run_binary(
    cmd: list[str],
    *,
    timeout: float = 5.0,
    env: dict[str, str] | None = None,
    allow_empty_exit: bool = True,
) -> bytes:
    """Run a subprocess and return its stdout as raw bytes."""
    return await _run_subprocess(cmd, timeout=timeout, env=env, allow_empty_exit=allow_empty_exit)


async def _run_with_stdin(
    cmd: list[str],
    input_data: bytes,
    *,
    timeout: float = 5.0,
    env: dict[str, str] | None = None,
) -> None:
    """Run a subprocess, piping input_data to its stdin.

    stdout is always sent to /dev/null. stderr is captured when
    MCP_CLIPBOARD_DEBUG=1 (for inclusion in error messages) but sent
    to /dev/null otherwise -- clipboard write commands (wl-copy, xclip)
    fork a background child that inherits pipe file descriptors, and
    piping stderr would cause communicate() to block until the child
    closes them.
    """
    debug = os.environ.get("MCP_CLIPBOARD_DEBUG", "") == "1"
    stderr_mode = asyncio.subprocess.PIPE if debug else asyncio.subprocess.DEVNULL
    proc: asyncio.subprocess.Process | None = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=stderr_mode,
                env=env,
            )
        except FileNotFoundError as fnf:
            raise ClipboardError(f"Command not found: {cmd[0]}") from fnf
        try:
            _, stderr_data = await asyncio.wait_for(
                proc.communicate(input=input_data), timeout=timeout
            )
        except TimeoutError as te:
            proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            raise ClipboardError(f"Clipboard command timed out: {' '.join(cmd)}") from te

        if proc.returncode != 0:
            msg = f"Clipboard write failed (rc={proc.returncode}): {cmd[0]}"
            if debug and stderr_data:
                msg += f"\nstderr: {stderr_data.decode(errors='replace').strip()}"
            raise ClipboardError(msg)
    finally:
        # See _run_subprocess: belt-and-suspenders cleanup for the
        # CancelledError path which bypasses except handlers entirely.
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()


def _find_wayland_display() -> str | None:
    """Find an available Wayland compositor socket.

    Scans $XDG_RUNTIME_DIR (falling back to /run/user/<uid>) for wayland-*
    Unix domain sockets.  Returns the socket name (e.g. "wayland-0") or None.
    """
    # os.getuid is POSIX-only (KeyError-free on Linux/macOS, AttributeError on
    # Windows). The Wayland path is never the active backend on Windows so
    # the fallback construction is dead anyway, but the test suite mocks
    # _get_backend to "wayland" on every platform; keep this Windows-safe.
    getuid = getattr(os, "getuid", lambda: 0)
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{getuid()}")
    runtime_dir = Path(xdg_runtime)
    if not runtime_dir.is_dir():
        return None

    try:
        sockets = sorted(
            entry.name
            for entry in runtime_dir.iterdir()
            if entry.name.startswith("wayland-")
            and not entry.name.endswith(".lock")
            and stat.S_ISSOCK(entry.stat().st_mode)
        )
    except OSError:
        return None

    return sockets[0] if sockets else None


def _wayland_env() -> dict[str, str] | None:
    """Build a subprocess env dict that ensures wl-paste can connect.

    wl-paste (via libwayland) needs XDG_RUNTIME_DIR to locate the compositor
    socket, and optionally WAYLAND_DISPLAY to pick which one.  When launched by
    an app launcher like Claude Desktop, both may be stripped from the
    environment.

    Returns None (inherit parent env) when both vars are already set.
    Otherwise returns a copy of os.environ with the missing vars injected.
    """
    has_display = bool(os.environ.get("WAYLAND_DISPLAY"))
    has_runtime = bool(os.environ.get("XDG_RUNTIME_DIR"))

    if has_display and has_runtime:
        return None  # both set — let subprocess inherit normally

    # Determine the runtime dir (needed by wl-paste even if WAYLAND_DISPLAY is set)
    # os.getuid is POSIX-only -- fall back to 0 on Windows so the path simply
    # fails the is_dir() check below and we return None.
    getuid = getattr(os, "getuid", lambda: 0)
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{getuid()}"
    if not Path(xdg_runtime).is_dir():
        return None

    env = os.environ.copy()

    if not has_runtime:
        env["XDG_RUNTIME_DIR"] = xdg_runtime

    if not has_display:
        display = _find_wayland_display()
        if display:
            env["WAYLAND_DISPLAY"] = display

    return env


def _detect_backend() -> str:
    """Detect which clipboard backend to use.

    Returns one of: 'wayland', 'x11', 'macos', 'windows'.
    Raises ClipboardError if no supported backend is found.
    """
    system = platform.system()

    if system == "Linux":
        session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
        wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
        has_wl_paste = shutil.which("wl-paste")

        # Prefer Wayland if env vars indicate it, or if a compositor socket exists
        if has_wl_paste and (
            session_type == "wayland" or wayland_display or _find_wayland_display()
        ):
            return "wayland"
        if shutil.which("xclip"):
            return "x11"
        raise ClipboardError(
            "No clipboard tool found. Install wl-paste (Wayland) or xclip (X11).\n"
            "  Fedora: sudo dnf install wl-clipboard   # or xclip\n"
            "  Ubuntu: sudo apt install wl-clipboard    # or xclip"
        )
    if system == "Darwin":
        return "macos"
    if system == "Windows":
        return "windows"

    raise ClipboardError(f"Unsupported platform: {system}")


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------

# Selections supported by the X11 + Wayland read paths. "clipboard" is the
# default Ctrl-C buffer; "primary" is the X11 PRIMARY selection (middle-click
# / select-text-to-paste) which Wayland mirrors via wl-paste --primary on
# most compositors. macOS and Windows have no analog and reject "primary".
_VALID_SELECTIONS: frozenset[str] = frozenset({"clipboard", "primary"})


def _wayland_primary_args(selection: str) -> list[str]:
    """Return the wl-paste/wl-copy --primary flag (or empty list)."""
    return ["--primary"] if selection == "primary" else []


def _validate_selection(selection: str) -> None:
    if selection not in _VALID_SELECTIONS:
        raise ClipboardError(
            f"Invalid selection: {selection!r}. Supported: {', '.join(sorted(_VALID_SELECTIONS))}"
        )


async def _wayland_read(mime_type: str, selection: str = "clipboard") -> str:
    _validate_selection(selection)
    args = ["wl-paste", "--type", mime_type, *_wayland_primary_args(selection)]
    return await _run(args, env=_wayland_env())


async def _wayland_list_formats(selection: str = "clipboard") -> list[str]:
    _validate_selection(selection)
    args = ["wl-paste", "--list-types", *_wayland_primary_args(selection)]
    raw = await _run(args, env=_wayland_env())
    return [line.strip() for line in raw.splitlines() if line.strip()]


async def _x11_read(mime_type: str, selection: str = "clipboard") -> str:
    _validate_selection(selection)
    return await _run(["xclip", "-selection", selection, "-target", mime_type, "-o"])


async def _x11_list_formats(selection: str = "clipboard") -> list[str]:
    _validate_selection(selection)
    raw = await _run(["xclip", "-selection", selection, "-target", "TARGETS", "-o"])
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _reject_non_clipboard_selection(selection: str, platform_name: str) -> None:
    """macOS and Windows have no PRIMARY-selection analog. Reject explicitly
    so the host model gets a clean error rather than silently falling back
    to the system clipboard (which would mask intent mismatches)."""
    if selection != "clipboard":
        raise ClipboardError(
            f"{platform_name} does not support selection={selection!r}; "
            f"only the 'clipboard' selection exists on this platform."
        )


async def _macos_read(mime_type: str, selection: str = "clipboard") -> str:
    _reject_non_clipboard_selection(selection, "macOS")
    if mime_type == "text/html":
        # osascript to get HTML from clipboard
        script = (
            'use framework "AppKit"\n'
            "set pb to current application's NSPasteboard's generalPasteboard()\n"
            'set htmlData to pb\'s dataForType:"public.html"\n'
            'if htmlData is missing value then return ""\n'
            "set htmlString to (current application's NSString's alloc()'s "
            "initWithData:htmlData encoding:(current application's NSUTF8StringEncoding))\n"
            "return htmlString as text"
        )
        return await _run(["osascript", "-e", script], allow_empty_exit=False)

    if mime_type == "text/plain":
        return await _run(["pbpaste"], allow_empty_exit=False)

    if mime_type == "text/rtf":
        script = (
            'use framework "AppKit"\n'
            "set pb to current application's NSPasteboard's generalPasteboard()\n"
            'set rtfData to pb\'s dataForType:"public.rtf"\n'
            'if rtfData is missing value then return ""\n'
            "set rtfString to (current application's NSString's alloc()'s "
            "initWithData:rtfData encoding:(current application's NSUTF8StringEncoding))\n"
            "return rtfString as text"
        )
        return await _run(["osascript", "-e", script], allow_empty_exit=False)

    if mime_type == "image/svg+xml":
        # SVG is written via _macos_write_typed under the "public.svg-image"
        # UTI (matching the Inkscape/Figma/browser convention). Reading it
        # back uses the same UTI; the bytes are UTF-8 XML, decoded as text.
        script = (
            'use framework "AppKit"\n'
            "set pb to current application's NSPasteboard's generalPasteboard()\n"
            'set svgData to pb\'s dataForType:"public.svg-image"\n'
            'if svgData is missing value then return ""\n'
            "set svgString to (current application's NSString's alloc()'s "
            "initWithData:svgData encoding:(current application's NSUTF8StringEncoding))\n"
            "return svgString as text"
        )
        return await _run(["osascript", "-e", script], allow_empty_exit=False)

    # Unsupported MIME type — signal "not available" rather than returning wrong content
    return ""


_UTI_TO_MIME: dict[str, str] = {
    "public.html": "text/html",
    "public.utf8-plain-text": "text/plain",
    "public.plain-text": "text/plain",
    "public.rtf": "text/rtf",
    "public.svg-image": "image/svg+xml",
    "public.png": "image/png",
    "public.tiff": "image/tiff",
    "public.jpeg": "image/jpeg",
    "public.url": "text/uri-list",
}


async def _macos_list_formats(selection: str = "clipboard") -> list[str]:
    _reject_non_clipboard_selection(selection, "macOS")
    script = (
        'use framework "AppKit"\n'
        "set pb to current application's NSPasteboard's generalPasteboard()\n"
        "set theTypes to pb's types() as list\n"
        'set output to ""\n'
        "repeat with t in theTypes\n"
        "  set output to output & (t as text) & linefeed\n"
        "end repeat\n"
        "return output"
    )
    raw = await _run(["osascript", "-e", script], allow_empty_exit=False)
    native = [line.strip() for line in raw.splitlines() if line.strip()]
    seen: set[str] = set()
    result: list[str] = []
    for t in native:
        mime = _UTI_TO_MIME.get(t, t)
        if mime not in seen:
            seen.add(mime)
            result.append(mime)
    return result


# Windows backend: direct Win32 clipboard API via pywin32.
#
# Earlier versions of this server shelled out to `powershell -NoProfile
# -Command "..."` per MCP tool call. That had two structural problems:
#   1. Cross-process read-after-write race -- after a SetDataObject(.., copy=true)
#      the OLE clipboard chain needs to fully propagate the snapshot from the
#      exiting writer before a fresh reader process sees it. The resulting
#      staleness window made `clipboard_copy(mime_type=image/svg+xml)` appear
#      to silently no-op when followed by an immediate `paste`/`list_formats`/
#      `read_raw` -- the bytes were on the clipboard, but the next reader saw
#      the previous state. (cmeans/mcp-clipboard#143)
#   2. PowerShell stdin/stdout codepage transcoding -- non-ASCII codepoints
#      lossy because [Console]::InputEncoding / OutputEncoding default to the
#      parent's active console code page. Closed by #131 (input) and #142 /
#      #132 (output) but kept fragile.
#
# The new backend keeps clipboard ownership inside the long-lived MCP-server
# Python process. Each clipboard op is a synchronous Win32 OpenClipboard ->
# Empty/Set/Get -> CloseClipboard transaction in our address space, dispatched
# off the asyncio loop via asyncio.to_thread. No subprocess spawn, no codepage
# transcoding, no cross-process race.
#
# See clipboard_win32.py for the wrapper module (imported at the top of the
# file as `_w32`).


async def _windows_read(mime_type: str, selection: str = "clipboard") -> str:
    _reject_non_clipboard_selection(selection, "Windows")
    if mime_type not in ("text/html", "text/plain", "text/rtf", "image/svg+xml"):
        # Unsupported MIME type — signal "not available" rather than returning wrong content
        return ""
    try:
        return await asyncio.to_thread(_w32.read_text, mime_type)
    except Exception as exc:
        raise ClipboardError(f"Windows clipboard read failed: {exc}") from exc


_WIN_TO_MIME: dict[str, str] = {
    "HTML Format": "text/html",
    "Text": "text/plain",
    "UnicodeText": "text/plain",
    "Rich Text Format": "text/rtf",
    "PNG": "image/png",
    "Bitmap": "image/bmp",
}


async def _windows_list_formats(selection: str = "clipboard") -> list[str]:
    _reject_non_clipboard_selection(selection, "Windows")
    try:
        native = await asyncio.to_thread(_w32.list_formats)
    except Exception as exc:
        raise ClipboardError(f"Windows clipboard list failed: {exc}") from exc
    # Deduplicate: Windows clipboards routinely expose both "Text" and
    # "UnicodeText" which both map to text/plain. Mirror _macos_list_formats.
    seen: set[str] = set()
    result: list[str] = []
    for f in native:
        mime = _WIN_TO_MIME.get(f, f)
        if mime not in seen:
            seen.add(mime)
            result.append(mime)
    return result


# ---------------------------------------------------------------------------
# Backend image readers (binary)
# ---------------------------------------------------------------------------


async def _wayland_read_image(mime_type: str, selection: str = "clipboard") -> bytes:
    _validate_selection(selection)
    args = ["wl-paste", "--type", mime_type, *_wayland_primary_args(selection)]
    return await _run_binary(args, env=_wayland_env())


async def _x11_read_image(mime_type: str, selection: str = "clipboard") -> bytes:
    _validate_selection(selection)
    return await _run_binary(["xclip", "-selection", selection, "-target", mime_type, "-o"])


async def _macos_read_image(mime_type: str, selection: str = "clipboard") -> bytes:
    _reject_non_clipboard_selection(selection, "macOS")
    # Map MIME to UTI for NSPasteboard lookup -- reject unknown types
    # to prevent AppleScript injection via crafted MIME strings (#24)
    mime_to_uti = {v: k for k, v in _UTI_TO_MIME.items() if v.startswith("image/")}
    uti = mime_to_uti.get(mime_type)
    if uti is None:
        raise ClipboardError(f"Unsupported image type: {mime_type}")
    # Return image data as base64 text via osascript, then decode to bytes
    script = (
        'use framework "AppKit"\n'
        'use framework "Foundation"\n'
        "set pb to current application's NSPasteboard's generalPasteboard()\n"
        f'set imgData to pb\'s dataForType:"{uti}"\n'
        'if imgData is missing value then return ""\n'
        "set b64 to (imgData's base64EncodedStringWithOptions:0)\n"
        "return b64 as text"
    )
    b64_text = await _run(["osascript", "-e", script], allow_empty_exit=False)
    if not b64_text.strip():
        return b""
    return base64.b64decode(b64_text.strip())


_WINDOWS_IMAGE_FORMATS: dict[str, str] = {
    "image/png": "Png",
    "image/jpeg": "Jpeg",
    "image/bmp": "Bmp",
    "image/gif": "Gif",
    "image/tiff": "Tiff",
}


async def _windows_read_image(mime_type: str, selection: str = "clipboard") -> bytes:
    _reject_non_clipboard_selection(selection, "Windows")
    # Map MIME to .NET ImageFormat -- reject unknown types (#34)
    dotnet_format = _WINDOWS_IMAGE_FORMATS.get(mime_type)
    if dotnet_format is None:
        raise ClipboardError(f"Unsupported image type: {mime_type}")

    # Read clipboard image as base64 via PowerShell
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$img = [System.Windows.Forms.Clipboard]::GetImage(); "
        "if ($img -eq $null) { return }; "
        "$ms = New-Object System.IO.MemoryStream; "
        f"$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::{dotnet_format}); "
        "[Convert]::ToBase64String($ms.ToArray())"
    )
    b64_text = await _run(["powershell", "-NoProfile", "-Command", script], allow_empty_exit=False)
    if not b64_text.strip():
        return b""
    return base64.b64decode(b64_text.strip())


# ---------------------------------------------------------------------------
# Backend writers (text)
# ---------------------------------------------------------------------------


async def _wayland_write(content: str) -> None:
    await _run_with_stdin(["wl-copy"], content.encode(), env=_wayland_env())


async def _x11_write(content: str) -> None:
    await _run_with_stdin(["xclip", "-selection", "clipboard"], content.encode())


async def _macos_write(content: str) -> None:
    await _run_with_stdin(["pbcopy"], content.encode())


# NOTE: _WINDOWS_UTF8_PREAMBLE -- the PowerShell stdin-encoding preamble
# from #131 that closed #129 -- is no longer needed: text/plain writes go
# through clipboard_win32.write_text which sets CF_UNICODETEXT directly
# from a Python str, with no PowerShell process and no codepage transcoding
# in the path. Removed alongside the rest of the PowerShell-subprocess Windows
# write backend.


async def _windows_write(content: str) -> None:
    try:
        await asyncio.to_thread(_w32.write_text, content, "text/plain")
    except Exception as exc:
        raise ClipboardError(f"Windows clipboard write failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Backend typed writers (MIME-aware)
# ---------------------------------------------------------------------------


async def _wayland_write_typed(content: str, mime_type: str) -> None:
    await _run_with_stdin(["wl-copy", "--type", mime_type], content.encode(), env=_wayland_env())


async def _x11_write_typed(content: str, mime_type: str) -> None:
    await _run_with_stdin(
        ["xclip", "-selection", "clipboard", "-target", mime_type],
        content.encode(),
    )


# Text-shaped MIME types writable on macOS via NSPasteboard setData:forType:.
# Each maps to the Uniform Type Identifier the pasteboard expects. SVG is
# textual XML but lands under public.svg-image; that is the UTI Inkscape,
# Figma, and browsers look for to consume an SVG from the clipboard.
_MACOS_TYPED_WRITE_UTIS: dict[str, str] = {
    "text/html": "public.html",
    "text/rtf": "public.rtf",
    "image/svg+xml": "public.svg-image",
}


def _macos_pasteboard_script(payload: bytes, uti: str) -> str:
    """Build the AppleScript that base64-decodes ``payload`` into NSData and
    writes it to the system pasteboard under the given UTI.

    The script is meant to be piped to ``osascript -`` over stdin (NOT passed
    via ``-e``). That decouples the total script length from execve's argv
    cap (~1 MiB ARG_MAX on default macOS), so payloads up to the
    ``MCP_CLIPBOARD_MAX_*`` envelope are safe.

    The chunked-base64 splitting is still required: AppleScript's parser
    rejects single source lines longer than 32,767 chars regardless of how
    the script reached osascript, so a >~24 KB raw payload would otherwise
    fail to compile.
    """
    b64 = base64.b64encode(payload).decode("ascii")
    b64_chunks = [b64[i : i + _APPLESCRIPT_CHUNK] for i in range(0, len(b64), _APPLESCRIPT_CHUNK)]
    if not b64_chunks:
        b64_chunks = [""]
    b64_lines = [f'set b64 to "{b64_chunks[0]}"']
    for chunk in b64_chunks[1:]:
        b64_lines.append(f'set b64 to b64 & "{chunk}"')
    return (
        'use framework "AppKit"\n'
        'use framework "Foundation"\n'
        + "\n".join(b64_lines)
        + "\n"
        + "set decoded to (current application's NSData's alloc()'s "
        "initWithBase64EncodedString:b64 options:0)\n"
        "set pb to current application's NSPasteboard's generalPasteboard()\n"
        "pb's clearContents()\n"
        f'pb\'s setData:decoded forType:"{uti}"\n'
    )


async def _macos_write_typed(content: str, mime_type: str) -> None:
    if mime_type == "text/plain":
        await _run_with_stdin(["pbcopy"], content.encode())
        return

    uti = _MACOS_TYPED_WRITE_UTIS.get(mime_type)
    if uti is None:
        supported = ["text/plain", *sorted(_MACOS_TYPED_WRITE_UTIS)]
        raise ClipboardError(
            f"macOS clipboard write does not support MIME type {mime_type!r}. "
            f"Supported: {', '.join(supported)}"
        )

    # Pipe via `osascript -` to escape execve's ~1 MiB ARG_MAX cap.
    # See _macos_pasteboard_script.
    await _run_with_stdin(
        ["osascript", "-"],
        _macos_pasteboard_script(content.encode("utf-8"), uti).encode("utf-8"),
    )


# Extended UTI map covering text/plain too. The single-format _macos_write_typed
# path uses pbcopy for text/plain (a more direct route), but the multi-format
# script needs to set every MIME via NSPasteboard so we can compose them in
# one clearContents() + N x setData:forType: AppleScript.
_MACOS_MULTI_WRITE_UTIS: dict[str, str] = {
    "text/plain": "public.utf8-plain-text",
    **_MACOS_TYPED_WRITE_UTIS,
}


def _macos_pasteboard_multi_script(payloads: list[tuple[bytes, str]]) -> str:
    """Build an AppleScript that writes multiple (payload, UTI) pairs to the
    pasteboard atomically: one ``clearContents`` followed by N ``setData:forType:``
    calls inside a single osascript invocation.

    Like ``_macos_pasteboard_script``, the result is meant to be piped over
    ``osascript -`` so the total script length is not bounded by ARG_MAX. The
    chunked-base64 line-length workaround is preserved per UTI.
    """
    parts: list[str] = ['use framework "AppKit"', 'use framework "Foundation"']
    parts.append("set pb to current application's NSPasteboard's generalPasteboard()")
    parts.append("pb's clearContents()")

    for i, (payload, uti) in enumerate(payloads):
        b64 = base64.b64encode(payload).decode("ascii")
        b64_chunks = [
            b64[j : j + _APPLESCRIPT_CHUNK] for j in range(0, len(b64), _APPLESCRIPT_CHUNK)
        ]
        if not b64_chunks:
            b64_chunks = [""]
        b64_var = f"b64_{i}"
        decoded_var = f"decoded_{i}"
        parts.append(f'set {b64_var} to "{b64_chunks[0]}"')
        for chunk in b64_chunks[1:]:
            parts.append(f'set {b64_var} to {b64_var} & "{chunk}"')
        parts.append(
            f"set {decoded_var} to (current application's NSData's alloc()'s "
            f"initWithBase64EncodedString:{b64_var} options:0)"
        )
        parts.append(f'pb\'s setData:{decoded_var} forType:"{uti}"')

    return "\n".join(parts) + "\n"


def _windows_html_clipboard_wrap(html: str) -> str:
    """Wrap HTML in the Windows CF_HTML clipboard format.

    The CF_HTML format requires a plain-text header containing byte offsets
    to the start/end of the HTML document and the selected fragment within it.
    Byte offsets are calculated after encoding to UTF-8.
    """
    marker_start = "<!--StartFragment-->"
    marker_end = "<!--EndFragment-->"
    body = f"<html><body>{marker_start}{html}{marker_end}</body></html>"

    header_template = (
        "Version:0.9\r\n"
        "StartHTML:{start_html:08d}\r\n"
        "EndHTML:{end_html:08d}\r\n"
        "StartFragment:{start_frag:08d}\r\n"
        "EndFragment:{end_frag:08d}\r\n"
    )
    # Measure header length using placeholder values (all same digit count)
    placeholder = header_template.format(start_html=0, end_html=0, start_frag=0, end_frag=0)
    header_len = len(placeholder.encode())
    body_bytes = body.encode()

    start_html = header_len
    start_frag = header_len + len(f"<html><body>{marker_start}".encode())
    end_frag = header_len + body_bytes.index(marker_end.encode())
    end_html = header_len + len(body_bytes)

    header = header_template.format(
        start_html=start_html,
        end_html=end_html,
        start_frag=start_frag,
        end_frag=end_frag,
    )
    return header + body


async def _windows_write_typed(content: str, mime_type: str) -> None:
    if mime_type not in ("text/plain", "text/html", "text/rtf", "image/svg+xml"):
        raise ClipboardError(
            f"Windows clipboard write does not support MIME type {mime_type!r}. "
            "Supported: text/plain, text/html, text/rtf, image/svg+xml"
        )

    # text/html: wrap as CF_HTML before handing to the Win32 writer, mirroring
    # what apps like Word, Outlook, and Chrome put on the clipboard. The
    # wrapper carries Version + StartHTML/EndHTML/StartFragment/EndFragment
    # offsets so paste targets can extract the body fragment from the
    # surrounding boilerplate.
    payload = _windows_html_clipboard_wrap(content) if mime_type == "text/html" else content

    try:
        await asyncio.to_thread(_w32.write_text, payload, mime_type)
    except Exception as exc:
        raise ClipboardError(f"Windows clipboard write failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Backend multi-format writers (#109)
# ---------------------------------------------------------------------------

# Order in which we'd prefer to write a single-MIME backend if more than one
# format is offered. text/html beats text/plain because Slack/Gmail/Discord
# render HTML; falling back to text/plain for non-HTML callers.
_SINGLE_MIME_PREFERENCE = ("text/html", "text/plain")


def _pick_single_mime(formats: dict[str, str]) -> tuple[str, str] | None:
    """Pick one (mime, content) from a multi-format dict for backends that
    can only carry one MIME per call (Wayland, X11). Returns ``None`` if the
    dict is empty.
    """
    for mime in _SINGLE_MIME_PREFERENCE:
        if mime in formats:
            return mime, formats[mime]
    # Fall back to whatever's first in the dict (Python 3.7+ insertion order).
    for mime, content in formats.items():
        return mime, content
    return None


async def _wayland_write_multi(formats: dict[str, str]) -> None:
    pick = _pick_single_mime(formats)
    if pick is None:
        return
    mime, content = pick
    await _run_with_stdin(["wl-copy", "--type", mime], content.encode("utf-8"), env=_wayland_env())


async def _x11_write_multi(formats: dict[str, str]) -> None:
    pick = _pick_single_mime(formats)
    if pick is None:
        return
    mime, content = pick
    await _run_with_stdin(
        ["xclip", "-selection", "clipboard", "-target", mime],
        content.encode("utf-8"),
    )


async def _macos_write_multi(formats: dict[str, str]) -> None:
    payloads: list[tuple[bytes, str]] = []
    for mime, content in formats.items():
        uti = _MACOS_MULTI_WRITE_UTIS.get(mime)
        if uti is None:
            # Skip unsupported MIMEs silently — multi-format is best-effort
            # by design; the caller may pass formats not all backends know.
            continue
        payloads.append((content.encode("utf-8"), uti))
    if not payloads:
        return
    await _run_with_stdin(
        ["osascript", "-"],
        _macos_pasteboard_multi_script(payloads).encode("utf-8"),
    )


async def _windows_write_multi(formats: dict[str, str]) -> None:
    # Filter to the MIME types the Win32 backend can carry, wrapping
    # text/html in CF_HTML (Version + StartHTML/EndHTML/StartFragment/
    # EndFragment offsets) before handing off. Other MIMEs drop silently --
    # multi-format is best-effort by design (the caller may pass formats
    # not all backends know).
    payloads: dict[str, str] = {}
    for mime, content in formats.items():
        if mime == "text/html":
            payloads[mime] = _windows_html_clipboard_wrap(content)
        elif mime in ("text/plain", "text/rtf"):
            payloads[mime] = content
        # Anything else: skip.

    if not payloads:
        return
    try:
        await asyncio.to_thread(_w32.write_multi, payloads)
    except Exception as exc:
        raise ClipboardError(f"Windows clipboard multi-format write failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Backend image writers (binary)
# ---------------------------------------------------------------------------

# MIME types accepted by clipboard_copy_image. Limited to PNG and JPEG in
# v1: these are what users actually copy (screenshots, photos), and shipping
# pass-through avoids a re-encoding dependency. Other formats (GIF, WebP,
# TIFF, BMP) can be added later without taking on Pillow.
_WRITABLE_IMAGE_MIME_TO_UTI: dict[str, str] = {
    "image/png": "public.png",
    "image/jpeg": "public.jpeg",
}

WRITABLE_IMAGE_MIMES: frozenset[str] = frozenset(_WRITABLE_IMAGE_MIME_TO_UTI)


async def _wayland_write_image(data: bytes, mime_type: str) -> None:
    await _run_with_stdin(["wl-copy", "--type", mime_type], data, env=_wayland_env())


async def _x11_write_image(data: bytes, mime_type: str) -> None:
    await _run_with_stdin(
        ["xclip", "-selection", "clipboard", "-target", mime_type, "-i"],
        data,
    )


async def _macos_write_image(data: bytes, mime_type: str) -> None:
    uti = _WRITABLE_IMAGE_MIME_TO_UTI.get(mime_type)
    if uti is None:
        raise ClipboardError(
            f"macOS clipboard image write does not support MIME type {mime_type!r}. "
            f"Supported: {', '.join(sorted(_WRITABLE_IMAGE_MIME_TO_UTI))}"
        )
    # Pipe via `osascript -` over stdin to escape execve's ~1 MiB ARG_MAX
    # cap, which the prior `osascript -e <script>` form would have hit for
    # any image above ~750 KB after base64 framing. See _macos_pasteboard_script.
    await _run_with_stdin(
        ["osascript", "-"],
        _macos_pasteboard_script(data, uti).encode("utf-8"),
    )


async def _windows_write_image(data: bytes, mime_type: str) -> None:
    if mime_type not in WRITABLE_IMAGE_MIMES:
        raise ClipboardError(
            f"Windows clipboard image write does not support MIME type {mime_type!r}. "
            f"Supported: {', '.join(sorted(WRITABLE_IMAGE_MIMES))}"
        )
    # The base64 payload flows over stdin, NOT inline in the script: Windows
    # CreateProcess caps lpCommandLine at 32,767 chars, so interpolating the
    # base64 directly would fail at the OS layer for any image larger than
    # ~24 KB raw. Mirrors the stdin pattern in _windows_write_typed.
    # SetImage takes a System.Drawing.Image; FromStream auto-detects PNG vs
    # JPEG from the magic header, so no per-MIME branching is needed.
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$b64 = [Console]::In.ReadToEnd(); "
        "$bytes = [Convert]::FromBase64String($b64); "
        "$ms = New-Object System.IO.MemoryStream(,$bytes); "
        "$img = [System.Drawing.Image]::FromStream($ms); "
        "[System.Windows.Forms.Clipboard]::SetImage($img)"
    )
    b64 = base64.b64encode(data).decode("ascii")
    await _run_with_stdin(
        ["powershell", "-NoProfile", "-Command", script],
        b64.encode("ascii"),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Cache the detected backend for the process lifetime
_backend: str | None = None

_VALID_BACKENDS = frozenset({"wayland", "x11", "macos", "windows"})


def _get_backend() -> str:
    global _backend
    if _backend is None:
        override = os.environ.get("MCP_CLIPBOARD_BACKEND", "").strip().lower()
        if override:
            if override not in _VALID_BACKENDS:
                raise ClipboardError(
                    f"Invalid MCP_CLIPBOARD_BACKEND={override!r}. "
                    f"Valid values: {', '.join(sorted(_VALID_BACKENDS))}"
                )
            _backend = override
            logger.debug("Clipboard backend (override): %s", _backend)
        else:
            _backend = _detect_backend()
            logger.debug("Clipboard backend: %s", _backend)
    return _backend


def reset_backend_cache() -> None:
    """Clear the cached backend so the next call to _get_backend() re-detects.

    Intended for tests that need to switch backends or re-read
    MCP_CLIPBOARD_BACKEND mid-process. Production code should not need
    this -- the cache is a process-lifetime decision.
    """
    global _backend
    _backend = None


_READERS = {
    "wayland": _wayland_read,
    "x11": _x11_read,
    "macos": _macos_read,
    "windows": _windows_read,
}

_FORMAT_LISTERS = {
    "wayland": _wayland_list_formats,
    "x11": _x11_list_formats,
    "macos": _macos_list_formats,
    "windows": _windows_list_formats,
}

_IMAGE_READERS = {
    "wayland": _wayland_read_image,
    "x11": _x11_read_image,
    "macos": _macos_read_image,
    "windows": _windows_read_image,
}

_WRITERS = {
    "wayland": _wayland_write,
    "x11": _x11_write,
    "macos": _macos_write,
    "windows": _windows_write,
}

_TYPED_WRITERS = {
    "wayland": _wayland_write_typed,
    "x11": _x11_write_typed,
    "macos": _macos_write_typed,
    "windows": _windows_write_typed,
}

_IMAGE_WRITERS = {
    "wayland": _wayland_write_image,
    "x11": _x11_write_image,
    "macos": _macos_write_image,
    "windows": _windows_write_image,
}

_MULTI_WRITERS = {
    "wayland": _wayland_write_multi,
    "x11": _x11_write_multi,
    "macos": _macos_write_multi,
    "windows": _windows_write_multi,
}


async def read_clipboard(mime_type: str = "text/plain", selection: str = "clipboard") -> str:
    """Read the clipboard content in the specified MIME type.

    Returns an empty string if the requested format is not available.

    Clipboard MIME types may include parameters (e.g.,
    ``text/plain;charset=utf-8``).  If the exact requested type is not
    found, this function falls back to listing available formats and
    retrying with a matching suffixed variant.

    ``selection`` selects which X11/Wayland buffer to read: ``"clipboard"``
    (default, the Ctrl-C buffer) or ``"primary"`` (X11 PRIMARY / middle-
    click selection; Wayland's analogous primary selection on most
    compositors). macOS and Windows have no PRIMARY analog and raise
    :exc:`ClipboardError` for any non-default selection. (#110)
    """
    backend = _get_backend()
    result = await _READERS[backend](mime_type, selection)

    # Wayland / X11 pass the MIME type verbatim to wl-paste / xclip which
    # may do strict matching.  Resolve via format listing when needed.
    if not result and backend in ("wayland", "x11"):
        base = base_mime_type(mime_type)
        formats = await _FORMAT_LISTERS[backend](selection)
        for fmt in formats:
            if fmt != mime_type and base_mime_type(fmt) == base:
                result = await _READERS[backend](fmt, selection)
                if result:
                    break

    return result


async def list_clipboard_formats(selection: str = "clipboard") -> list[str]:
    """Return the list of MIME/format types currently available on the clipboard.

    ``selection`` is ``"clipboard"`` (default) or ``"primary"`` — see
    :func:`read_clipboard` for the per-platform contract. (#110)
    """
    backend = _get_backend()
    return await _FORMAT_LISTERS[backend](selection)


async def read_clipboard_image(mime_type: str = "image/png", selection: str = "clipboard") -> bytes:
    """Read binary image data from the clipboard.

    Returns raw bytes of the image, or empty bytes if not available.

    Like :func:`read_clipboard`, falls back to a matching suffixed MIME
    type when the exact requested type is not available, and accepts a
    ``selection`` parameter (``"clipboard"`` default; ``"primary"`` on
    Wayland/X11). (#110)

    Raises :exc:`ClipboardSizeError` when the image exceeds
    ``MCP_CLIPBOARD_MAX_IMAGE_BYTES`` (default 10 MB).
    """
    backend = _get_backend()
    result = await _IMAGE_READERS[backend](mime_type, selection)

    if not result and backend in ("wayland", "x11"):
        base = base_mime_type(mime_type)
        formats = await _FORMAT_LISTERS[backend](selection)
        for fmt in formats:
            if fmt != mime_type and base_mime_type(fmt) == base:
                result = await _IMAGE_READERS[backend](fmt, selection)
                if result:
                    break

    if len(result) > _MAX_IMAGE_BYTES:
        raise ClipboardSizeError(
            f"Image exceeds clipboard read limit "
            f"({len(result):,} bytes, max {_MAX_IMAGE_BYTES:,}). "
            f"Set MCP_CLIPBOARD_MAX_IMAGE_BYTES to increase."
        )
    return result


async def write_clipboard(content: str) -> None:
    """Write plain text to the system clipboard."""
    backend = _get_backend()
    await _WRITERS[backend](content)


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _validate_image_magic(data: bytes, mime_type: str) -> None:
    """Verify the bytes start with the expected magic for the declared MIME.

    Catches the host model declaring an image MIME for non-image bytes and
    catches PNG-vs-JPEG mismatches before they reach the OS pasteboard, where
    a wrong-format paste can be confusing or crash some receiving apps.
    """
    if mime_type == "image/png" and not data.startswith(_PNG_MAGIC):
        raise ClipboardError(
            "image_data does not have a PNG header; declared mime_type='image/png'"
        )
    if mime_type == "image/jpeg" and not data.startswith(_JPEG_MAGIC):
        raise ClipboardError(
            "image_data does not have a JPEG header; declared mime_type='image/jpeg'"
        )


async def write_clipboard_image(data: bytes, mime_type: str) -> None:
    """Write a binary image to the system clipboard.

    Accepts ``image/png`` and ``image/jpeg`` only in v1. Bytes are passed
    through to the platform clipboard with no re-encoding. Magic bytes are
    validated against the declared MIME type, and total size is capped at
    ``MCP_CLIPBOARD_MAX_IMAGE_BYTES`` (default 10 MB).

    Raises :exc:`ClipboardError` for unsupported MIME types or mismatched
    headers, and :exc:`ClipboardSizeError` when the payload exceeds the cap.
    """
    if mime_type not in WRITABLE_IMAGE_MIMES:
        raise ClipboardError(
            f"Unsupported image MIME type: {mime_type!r}. "
            f"Supported: {', '.join(sorted(WRITABLE_IMAGE_MIMES))}"
        )
    if len(data) > _MAX_IMAGE_BYTES:
        raise ClipboardSizeError(
            f"Image exceeds clipboard write limit "
            f"({len(data):,} bytes, max {_MAX_IMAGE_BYTES:,}). "
            f"Set MCP_CLIPBOARD_MAX_IMAGE_BYTES to increase."
        )
    _validate_image_magic(data, mime_type)

    backend = _get_backend()
    await _IMAGE_WRITERS[backend](data, mime_type)


async def write_clipboard_typed(content: str, mime_type: str) -> None:
    """Write content to the system clipboard with an explicit MIME type.

    On Wayland and X11, any text MIME type is passed through to the
    underlying tool (``wl-copy --type`` / ``xclip -target``).  On macOS
    and Windows, only ``text/plain``, ``text/html``, and ``text/rtf`` are
    supported; other types raise :exc:`ClipboardError`.

    Note: Wayland and X11 write a single MIME type per call.  Writing
    multiple types atomically (e.g. both ``text/html`` and ``text/plain``)
    requires owning the clipboard selection across calls, which is not
    supported by this implementation.
    """
    backend = _get_backend()
    await _TYPED_WRITERS[backend](content, mime_type)


async def write_clipboard_multi_format(formats: dict[str, str]) -> None:
    """Write multiple MIME-keyed text payloads to the clipboard.

    On macOS and Windows, all supported entries land on the clipboard
    atomically — a paste target picks the format it prefers (Slack/Gmail
    pick ``text/html``; vim/terminal pick ``text/plain``).

    On Wayland and X11, the underlying ``wl-copy``/``xclip`` tools only
    carry a single MIME per invocation, so this function picks the
    highest-preference format from the dict (``text/html`` > ``text/plain``
    > whatever's first) and writes only that one. Other formats are
    dropped — callers needing a plain-text fallback on Linux must call
    ``write_clipboard`` separately.

    Unknown MIMEs on macOS/Windows are dropped silently. Multi-format
    write is best-effort by design; failures to set one MIME do not
    prevent setting others.
    """
    backend = _get_backend()
    await _MULTI_WRITERS[backend](formats)
