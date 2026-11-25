r"""Direct Win32 clipboard backend via pywin32.

Replaces the PowerShell-subprocess-per-call backend used in earlier versions
of this server. The subprocess approach had two structural problems:

1. **Cross-process read-after-write race.** Each MCP tool call spawned a
   fresh ``powershell -NoProfile -Command "..."`` instance, opened the
   clipboard, did its op, and exited. After a SetDataObject(.., copy=true)
   the OLE clipboard chain needs to fully propagate the snapshot from the
   exiting writer process before a fresh reader process sees it. The
   resulting staleness window made `clipboard_copy(mime_type=image/svg+xml)`
   appear to silently no-op when followed by an immediate `paste` /
   `list_formats` / `read_raw` -- the bytes were on the clipboard, but
   the next reader saw the previous state.
2. **PowerShell stdin / stdout codepage transcoding.** Bytes piped between
   Python and PowerShell went through `[Console]::InputEncoding` /
   `OutputEncoding` which default to the parent's active console code page
   (typically CP1252 on US-English Windows). Non-ASCII codepoints were
   transliterated or substituted with U+003F. Closed-but-recurring (#129
   on the input side, #142/#132 on the output side).

This module keeps clipboard ownership inside the long-lived MCP-server
Python process. All clipboard operations happen synchronously in our own
address space. No subprocess spawn and no codepage transcoding. Each
write is atomic between OpenClipboard and CloseClipboard; clipboard
chain observers can still overwrite it afterward (see below).

**Write path: raw SetClipboardData with GlobalAlloc(GMEM_MOVEABLE) HGLOBALs.**
This is the write pattern Microsoft documents for SetClipboardData,
and the one Chromium uses (`ui/base/clipboard/clipboard_win.cc`). The
flow is:

  OpenClipboard(owner_hwnd) -> EmptyClipboard() ->
    for each (format_id, payload):
      h = GlobalAlloc(GMEM_MOVEABLE, len(payload))
      memcpy(GlobalLock(h), payload, len(payload)); GlobalUnlock(h)
      SetClipboardData(format_id, h)   # system takes ownership
  -> CloseClipboard()

No OLE, no IDataObject, no hidden OLE-managed window, no delayed
rendering, no message pump, no post-write verify, no retry on
SetClipboardData itself.

**Why no post-write verify-retry tax.** The Windows clipboard chain
is racy by design. Per the [Microsoft Q&A on WM_CLIPBOARDUPDATE]
(https://learn.microsoft.com/en-us/answers/questions/1327362/wm-clipboardupdate-issue)
the notification is asynchronously POSTED on Windows 11, so
observers (Clipboard History service `cbdhsvc`, Cloud Clipboard,
Suggested Actions text extractor, OneDrive, antivirus, third-party
clipboard managers) can re-open the clipboard immediately after our
CloseClipboard and overwrite our content. During #146 a
.NET-shaped 10x100ms post-write verify-retry was tried on top of this
path. QEMU testing showed it made the race STRICTLY WORSE (the race
reruns went from 4/6 PASS to 0/6 PASS) because each verify pair
generates an additional WM_CLIPBOARDUPDATE notification that amplifies
observer contention, so it was removed. The .NET WinForms
`Clipboard.SetDataObject(data, copy, 10, 100)` retry often cited as
"the standard tax" is on the OpenClipboard contention side, not a
post-write verify.

The race remains: on Windows 11 with Clipboard History on (the
default), an observer can overwrite a write. For test machines only,
dev/windows-clipboard-observers.ps1 switches the documented Microsoft
observers off. It is a test-VM tool, not a fix for users: it needs
Administrator, changes machine-wide policy for every user, stops
services and asks for a reboot.

A passive sequence-number canary at the write boundary (two
`GetClipboardSequenceNumber` calls bracketing the
Open/Empty/Set/Close transaction) was also tried during #146 as an
observation-only diagnostic. QEMU re-tested with chain observers
DISABLED to isolate its effect: 0/6 race PASS and six in-suite
first-attempt recoveries, on a setup that without the canary produced
6/6 PASS and zero recoveries. Even "passive" instrumentation measurably
changed the race timing on real Windows 11, so it was removed. Do not
add work to the write hot path even when it looks free.

**Why not OleSetClipboard / OleFlushClipboard?** OLE is for delayed
rendering, cross-process drag-drop, and IDataObject-based marshaling.
We have all bytes upfront for every format, so we need none of that.
The OLE path additionally creates a hidden CLIPBRDWNDCLASS window that
hosts WM_RENDERFORMAT / WM_DESTROYCLIPBOARD message handling, and that
window lives on the thread that called OleSetClipboard. In a long-
lived MCP server with no UI message pump, those messages queue forever;
consumers (clipboard managers, OneDrive shell extensions, antivirus)
that walk our formats hit a 30-second WM_RENDERFORMAT timeout and
synthesize their own clipboard copy in self-defense -- presenting to
us as "our write was silently overwritten" (the e2e flake seen while
an OLE-based write path was tried during #146). Raw
SetClipboardData with materialized HGLOBALs has no hidden window and
no rendering round-trip, eliminating the entire failure mode.

**Why GMEM_MOVEABLE specifically?** Per MSDN SetClipboardData: "If the
hMem parameter identifies a memory object, the object must have been
allocated using the function with the GMEM_MOVEABLE flag." A
GMEM_FIXED handle is silently rejected (SetClipboardData returns
non-NULL but no consumer can read the format -- the same silent
no-op symptom). pywin32's high-level `SetClipboardData(fmt, bytes)`
wrapper does allocate GMEM_MOVEABLE internally, so the silent no-op
seen on the pre-OLE write path (before this rewrite) was a separate
bug -- the clipboard-owner window problem, fixed during #146 by
opening the clipboard with a process-owned message-only window.

**Read path: OpenClipboard + GetClipboardData.** Unchanged from
earlier revisions. OLE was never required for reads; only the
cross-process write race motivated the temporary OleSetClipboard
detour.

Module imports are deferred to function bodies so this file can be parsed
on non-Windows platforms (CI runs on Linux). Calling any function on a
non-Windows host raises ``ImportError`` immediately rather than producing
a confusing pywin32 error later.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


# Public API contract (kept stable so dispatch in clipboard.py does not
# need to know the backend implementation):
#
#   read_text(mime_type: str) -> str
#       Returns "" if the format is not on the clipboard. Caller decides
#       whether absence is an error.
#
#   write_text(content: str, mime_type: str) -> None
#       Replaces the clipboard with the given (mime, text) pair via
#       OpenClipboard + EmptyClipboard + SetClipboardData + CloseClipboard.
#
#   write_multi(formats: dict[str, str]) -> None
#       Replaces the clipboard with all (mime, text) pairs in one
#       Empty + Set..Set + Close transaction. Atomic by construction.
#       Used for clipboard_copy_markdown's text/html + text/plain combo.
#
#   list_formats() -> list[str]
#       Returns native format names. Caller maps to MIME via _WIN_TO_MIME
#       in clipboard.py.
#
# Image read/write stays on the PowerShell backend in Phase 1; will move
# here in a follow-up PR with DIB <-> PNG conversion.


# --- HGLOBAL allocation via ctypes ----------------------------------------
#
# SetClipboardData requires its HANDLE argument to be a GMEM_MOVEABLE
# global memory handle that the SYSTEM takes ownership of on success.
# Driving the allocation ourselves via ctypes (rather than going through
# pywin32's SetClipboardData(fmt, bytes) helper) makes the GMEM_MOVEABLE
# flag explicit and removes any ambiguity about what allocation strategy
# the wrapper happens to be using. Unlike Chromium's `CreateGlobalData()`,
# which allocates one extra character and writes a NUL terminator, the
# payload is copied as-is: callers that need a terminator include it.

_GMEM_MOVEABLE = 0x0002
_GMEM_ZEROINIT = 0x0040


def _kernel32() -> Any:
    """Return ctypes.windll.kernel32 with GlobalAlloc / GlobalLock /
    GlobalUnlock / GlobalFree argtypes set up.

    Calling this on every write keeps the wrapper stateless (no module-
    level cached handle) so the test fixture's per-test ctypes.windll
    teardown stays clean. The argtypes / restype assignments are
    idempotent; setting them per call is microseconds.
    """
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # Windows-only
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    return kernel32


def _allocate_hglobal(payload: bytes) -> int:
    """Allocate a GMEM_MOVEABLE HGLOBAL, copy `payload` into it, and
    return the integer handle.

    The caller transfers ownership to SetClipboardData on success
    (the system owns the handle and frees it when the clipboard is next
    emptied; the data outlives this process). On any failure path BEFORE
    SetClipboardData succeeds, the caller MUST GlobalFree the handle
    we return -- the system has not taken ownership yet.

    Raises MemoryError if GlobalAlloc returns NULL (out-of-memory).
    Raises OSError if GlobalLock returns NULL (handle invalid; should
    never happen for a fresh GMEM_MOVEABLE allocation, defensive).
    """
    kernel32 = _kernel32()
    # GlobalAlloc(GMEM_MOVEABLE, 0) is well-defined but pointless; round
    # up to 1 byte so SetClipboardData has a real handle to take. That
    # byte gets no payload copied into it, so allocate it zeroed: an
    # empty RTF / SVG write then reads back as "" (read_text strips a
    # trailing NUL) instead of one uninitialized byte.
    size = len(payload) if payload else 1
    flags = _GMEM_MOVEABLE if payload else _GMEM_MOVEABLE | _GMEM_ZEROINIT
    handle = kernel32.GlobalAlloc(flags, size)
    if not handle:
        raise MemoryError(
            f"GlobalAlloc({flags:#06x}, {size}) returned NULL (out of global memory)."
        )
    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        raise OSError(
            "GlobalLock returned NULL for a freshly-allocated GMEM_MOVEABLE "
            "handle. This indicates handle corruption; should not happen."
        )
    if payload:
        ctypes.memmove(locked, payload, len(payload))
    # GlobalUnlock returns 0 when the lock count drops to 0, which is
    # expected for our one-lock-one-unlock pattern. The "failure" case
    # we would care about is GetLastError != ERROR_SUCCESS; for our
    # always-paired Lock/Unlock that doesn't arise, so the return
    # value is intentionally not inspected.
    kernel32.GlobalUnlock(handle)
    return int(handle)


def _user32() -> Any:
    """Return ctypes.windll.user32 with SetClipboardData argtypes ready.

    Like _kernel32, this is called per-write for stateless test-fixture
    behavior. SetClipboardData via ctypes (rather than pywin32) keeps
    the GMEM_MOVEABLE handle interpretation explicit -- the second
    argument is a HANDLE, not a Python-level bytes payload that some
    wrapper would re-allocate.
    """
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]  # Windows-only
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    return user32


def _set_clipboard_data(fmt_id: int, handle: int) -> None:
    """Call user32.SetClipboardData(fmt_id, handle).

    On success, the system takes ownership of the handle and we must
    NOT GlobalFree it. On failure (return value NULL), we still own
    the handle and the caller will GlobalFree it from the surrounding
    cleanup. Raises OSError on failure with the GetLastError code.
    """
    user32 = _user32()
    result = user32.SetClipboardData(ctypes.c_uint(fmt_id), ctypes.c_void_p(handle))
    if not result:
        # ctypes.get_last_error is Windows-only (since Python 3.3). On
        # Linux CI it doesn't exist; fall back to a sentinel so the
        # unit-test path doesn't crash on the diagnostic.
        err = getattr(ctypes, "get_last_error", lambda: 0)()
        raise OSError(
            f"SetClipboardData(fmt={fmt_id}, handle={handle:#x}) returned NULL "
            f"(GetLastError={err})."
        )


# --- Owner-window management ----------------------------------------------
#
# SetClipboardData requires the calling process to be the clipboard
# owner, which is set to the HWND passed to OpenClipboard at the time
# EmptyClipboard is called. The HWND must be a window owned by our
# process; NULL, the desktop, or any system-owned window will cause
# silent SetClipboardData failure for registered custom formats
# (image/svg+xml, "HTML Format", "Rich Text Format"). Built-in
# CF_UNICODETEXT survives the bad-owner path only because Windows
# synthesizes CF_TEXT / CF_OEMTEXT / CF_LOCALE from it regardless.

_owner_hwnd: int | None = None
_owner_hwnd_lock = threading.Lock()


def _get_clipboard_hwnd() -> int:
    """Return a process-owned HWND to associate with OpenClipboard.

    Critical: Per MSDN -- "If the application specifies a NULL window handle
    when opening the clipboard, EmptyClipboard succeeds but sets the
    clipboard owner to NULL. Note that this causes SetClipboardData to
    fail." But it goes deeper than that. SetClipboardData requires the
    *calling process* to be the clipboard owner. EmptyClipboard sets
    ownership to the window passed to OpenClipboard. So:

      - OpenClipboard(NULL) -> EmptyClipboard sets owner to NULL ->
        SetClipboardData fails for registered custom formats.
      - OpenClipboard(GetDesktopWindow()) -> EmptyClipboard sets owner to
        the desktop window (system-owned) -> our process is still NOT the
        owner -> SetClipboardData still fails.
      - OpenClipboard(<window-our-process-created>) -> EmptyClipboard sets
        owner to our window -> we ARE the owner -> SetClipboardData works
        for any format.

    Built-in formats (CF_UNICODETEXT) appear to survive both broken cases
    because their synthesis path (Windows auto-generates CF_TEXT,
    CF_OEMTEXT, CF_LOCALE from CF_UNICODETEXT regardless of caller
    ownership), bootstrapping the appearance of a successful write. The
    custom-format path has no synthesis fallback so silent failure is
    fully visible.

    The window is created lazily on first call and reused for the lifetime
    of the process (cached in module-level `_owner_hwnd`). DestroyWindow on
    process exit is implicit -- Windows tears down windows owned by the
    exiting process automatically. The data we put on the clipboard before
    process exit persists because SetClipboardData(format, handle) takes
    ownership of the global memory handle; the clipboard owner being torn
    down doesn't invalidate the data.

    Chromium uses a message-only window (base::win::MessageWindow) for
    the same purpose; ours is HWND_MESSAGE-parented, STATIC class,
    created once and cached.
    """
    global _owner_hwnd
    if _owner_hwnd is not None:
        return _owner_hwnd

    # Double-checked locking: clipboard.py dispatches the synchronous Win32
    # path through asyncio.to_thread, so two concurrent first-calls could
    # otherwise each race past the None-check and create two windows
    # (leaking one HWND per burst). Re-check inside the lock so only one
    # caller does the CreateWindowEx; the rest see the cached value.
    with _owner_hwnd_lock:
        if _owner_hwnd is not None:
            return _owner_hwnd

        import win32con  # type: ignore[import-not-found,import-untyped]
        import win32gui  # type: ignore[import-not-found,import-untyped]

        # CreateWindowEx with class="STATIC" (built-in USER32 class, no
        # registration needed) and parent=HWND_MESSAGE creates a message-only
        # window: invisible, top-level, and unable to receive UI input. The
        # other zero parameters mean default extended style, no menu, no
        # creation params. The handle returned is the HWND we use as the
        # clipboard owner for the rest of the process's lifetime.
        _owner_hwnd = int(
            win32gui.CreateWindowEx(
                0,  # extended style
                "STATIC",  # built-in class
                None,  # window name
                0,  # style
                0,  # x
                0,  # y
                0,  # width
                0,  # height
                win32con.HWND_MESSAGE,  # parent: message-only top-level
                0,  # menu
                0,  # hInstance
                None,  # creation params
            )
        )
        return _owner_hwnd


def _open_clipboard_with_retry(win32clipboard: Any, retries: int = 10, delay_ms: int = 50) -> None:
    """OpenClipboard fails with WindowsError when another process holds it.

    Chromium's ScopedClipboard::Acquire retries 5 attempts x 5 ms (25 ms
    ceiling). Microsoft's documented SetDataObject pattern is 10 x 100 ms
    (1 s ceiling). We split the difference at 10 x 50 ms (500 ms ceiling)
    because the bottleneck this fights is brief contention from clipboard
    inspectors / antivirus / clipboard managers, typically resolved in
    tens of milliseconds.

    Always passes a real HWND (a process-owned message-only window from
    _get_clipboard_hwnd) to OpenClipboard so our process is the clipboard
    owner; SetClipboardData then succeeds for registered custom formats.
    See _get_clipboard_hwnd for the detailed MSDN background.
    """
    hwnd = _get_clipboard_hwnd()

    last_err: Exception | None = None
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard(hwnd)
            return
        except Exception as exc:  # pywin32 raises pywintypes.error
            last_err = exc
            time.sleep(delay_ms / 1000.0)
    raise RuntimeError(
        f"OpenClipboard failed after {retries} retries (delay={delay_ms}ms each); "
        f"another process is holding the clipboard. Last error: {last_err!r}"
    )


def _import_win32clipboard() -> Any:
    """Defer the pywin32 import so this module can be parsed on Linux CI.

    Raises ImportError with a clear message on non-Windows platforms.
    """
    try:
        # On Linux / macOS pywin32 is not installed (the sys_platform marker
        # in pyproject.toml constrains it to Windows), so the module is
        # missing -> ImportError. On Windows pywin32 ships without type
        # stubs -- types-pywin32 exists on PyPI but adds an extra dep
        # purely for static analysis. Suppress both error codes here so
        # mypy is happy on Linux CI (import-not-found) and Windows CI
        # (import-untyped) without taking on the stub package.
        import win32clipboard  # type: ignore[import-not-found,import-untyped]
    except ImportError as exc:
        if sys.platform == "win32":
            # On Windows, pywin32 is a declared dependency, so an
            # ImportError here is a broken install (e.g. "DLL load
            # failed"). Keep the real cause in the message.
            raise ImportError(
                f"pywin32 failed to import ({exc}). It is a dependency of "
                "mcp-clipboard on Windows; reinstall mcp-clipboard to repair it."
            ) from exc
        raise ImportError(
            "clipboard_win32 requires pywin32, which is only installed on "
            "Windows. This module should not be invoked on non-Windows hosts."
        ) from exc
    return win32clipboard


def _register_format(name: str) -> int:
    """Look up (and cache) the registered clipboard format ID for `name`.

    Standard formats have fixed IDs in win32clipboard (CF_*); custom
    strings like 'image/svg+xml', 'HTML Format', 'Rich Text Format' get
    registered the first time they are seen, returning a stable integer
    in the 0xC000-0xFFFF range for the user session.
    """
    if name in _format_id_cache:
        return _format_id_cache[name]
    win32clipboard = _import_win32clipboard()
    # pywin32 has no type stubs in the configured set; cast to int so mypy
    # knows the cached value matches the declared return type.
    fmt_id: int = int(win32clipboard.RegisterClipboardFormat(name))
    _format_id_cache[name] = fmt_id
    return fmt_id


_format_id_cache: dict[str, int] = {}


# --- MIME -> Windows format mapping ----------------------------------------
#
# CF_UNICODETEXT carries UTF-16 LE; the Win32 docs specify it as the
# canonical Unicode text format. Using it for text/plain avoids the
# transcoding loss CF_TEXT (CP_ACP) and CF_OEMTEXT (CP_OEMCP) would impose.
#
# 'HTML Format' is the registered string-name for CF_HTML; the bytes are
# the CF_HTML wrapper produced by clipboard._windows_html_clipboard_wrap
# (Version, StartHTML / EndHTML offsets, etc.) encoded UTF-8.
#
# 'Rich Text Format' is the registered string-name for CF_RTF; bytes are
# UTF-8 RTF source.
#
# 'image/svg+xml' is a custom format string; bytes are UTF-8 SVG markup.

_MIME_TO_FORMAT_NAME: dict[str, str] = {
    "text/html": "HTML Format",
    "text/rtf": "Rich Text Format",
    "image/svg+xml": "image/svg+xml",
}


def _format_id_for_mime(win32clipboard: Any, mime_type: str) -> int:
    """Resolve a MIME type to its Win32 clipboard format ID."""
    if mime_type == "text/plain":
        # pywin32 has no type stubs in the configured set; cast to int.
        return int(win32clipboard.CF_UNICODETEXT)
    name = _MIME_TO_FORMAT_NAME.get(mime_type)
    if name is None:
        raise ValueError(f"Unsupported MIME type for Win32 clipboard: {mime_type!r}")
    return _register_format(name)


# --- Public sync API -------------------------------------------------------


def read_text(mime_type: str) -> str:
    """Read a text-shaped format from the clipboard. Returns "" if absent.

    text/plain reads CF_UNICODETEXT (UTF-16 LE); pywin32 returns a
    Python str directly via the Unicode-aware wrapper.

    text/html reads the registered "HTML Format"; the bytes are the
    full CF_HTML wrapper (Version + offsets + body) encoded UTF-8.
    Returned as the decoded UTF-8 string, header included, so
    clipboard_read_raw shows it as stored. clipboard_paste strips the
    header (parser.strip_cf_html_header) before parsing.

    text/rtf and image/svg+xml read their registered custom format
    strings; bytes are UTF-8 source.
    """
    win32clipboard = _import_win32clipboard()
    fmt_id = _format_id_for_mime(win32clipboard, mime_type)

    _open_clipboard_with_retry(win32clipboard)
    try:
        if not win32clipboard.IsClipboardFormatAvailable(fmt_id):
            return ""
        data = win32clipboard.GetClipboardData(fmt_id)
    finally:
        win32clipboard.CloseClipboard()

    # CF_UNICODETEXT comes back as a Python str directly. Custom registered
    # formats come back as bytes; decode UTF-8.
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
        # Our GMEM_MOVEABLE allocation for registered custom-format
        # payloads carries the source bytes exactly. Some consumers on
        # the read side append a trailing NUL when handing the buffer
        # back through pywin32's GetClipboardData wrapper; strip it so
        # callers see the exact source bytes regardless of who wrote
        # the clipboard.
        if raw.endswith(b"\x00"):
            raw = raw[:-1]
        return raw.decode("utf-8", errors="replace")
    # Defensive last-resort: pywin32 has historically returned other shapes
    # (memoryview, int) for unusual custom formats on some Windows versions.
    # Coerce to str so the caller never sees a non-str return; log so a
    # future debugging session can spot the unexpected pywin32 shape rather
    # than the str() rendering masking it.
    logger.debug(
        "GetClipboardData returned unexpected type %s for format -- coercing via str()",
        type(data).__name__,
    )
    return str(data)


def _encode_for_format(fmt_id: int, content: str, win32clipboard: Any) -> bytes:
    """Encode a Python str into the byte representation Windows expects for
    the given clipboard format ID.

    CF_UNICODETEXT requires null-terminated UTF-16 LE; the trailing wchar
    NUL is part of the format contract per MSDN. Custom registered formats
    (HTML Format, Rich Text Format, image/svg+xml) take raw UTF-8 bytes;
    their internal end-of-data conventions are format-specific (CF_HTML
    has Version + offset headers; SVG/RTF terminate at the closing tag).
    """
    if fmt_id == win32clipboard.CF_UNICODETEXT:
        return content.encode("utf-16-le") + b"\x00\x00"
    return content.encode("utf-8")


# --- Write transaction ----------------------------------------------------
#
# One Open + Empty + N x SetClipboardData + Close per write call, the
# pattern in Microsoft's SetClipboardData documentation. Retries happen
# only around OpenClipboard (_open_clipboard_with_retry, which retries any
# OpenClipboard failure; see #169); there is
# no retry on SetClipboardData and no post-write IsClipboardFormatAvailable
# verify. The post-write verify-retry and the sequence-number canary tried
# during #146 both made the observer race worse in QEMU testing; see the
# module docstring. The observer race itself remains;
# dev/windows-clipboard-observers.ps1 switches the observers off on test
# machines only (see the module docstring).


def _write_payloads(payloads: dict[int, bytes]) -> None:
    """Replace the clipboard with the given (format_id -> bytes) mapping
    via the canonical Win32 raw-SetClipboardData pattern.

    Allocates a GMEM_MOVEABLE HGLOBAL per format and hands ownership to
    SetClipboardData under a single Open/Empty/Close transaction. On
    success the system owns every handle. On any failure, allocated
    handles that did not transfer are GlobalFree'd.

    Atomicity: between OpenClipboard and CloseClipboard, no other
    process can EmptyClipboard or write its own data -- the OS
    serializes clipboard access via the per-clipboard mutex inside
    the kernel. Once CloseClipboard returns, our formats are visible
    to the entire system in one observable step (modulo the
    documented chain-observer race where observers can re-open the
    clipboard immediately AFTER our CloseClipboard and overwrite via
    their own WM_CLIPBOARDUPDATE reaction; that race is documented
    in the comment block above this function).
    """
    if not payloads:
        return

    win32clipboard = _import_win32clipboard()
    kernel32 = _kernel32()

    handles: list[tuple[int, int]] = []  # (format_id, handle)
    transferred: set[int] = set()
    try:
        for fmt_id, payload in payloads.items():
            handles.append((fmt_id, _allocate_hglobal(payload)))

        _open_clipboard_with_retry(win32clipboard)
        try:
            win32clipboard.EmptyClipboard()
            for fmt_id, handle in handles:
                _set_clipboard_data(fmt_id, handle)
                transferred.add(handle)
        finally:
            win32clipboard.CloseClipboard()
    finally:
        for _, handle in handles:
            if handle not in transferred:
                kernel32.GlobalFree(ctypes.c_void_p(handle))


def write_text(content: str, mime_type: str) -> None:
    """Replace the clipboard with a single (format, content) pair.

    Atomic via OpenClipboard + EmptyClipboard + SetClipboardData +
    CloseClipboard. The previous clipboard owner is fully replaced
    before this function returns. Clipboard chain observers can still
    overwrite the content after CloseClipboard (see the module
    docstring).
    """
    win32clipboard = _import_win32clipboard()
    fmt_id = _format_id_for_mime(win32clipboard, mime_type)
    encoded = _encode_for_format(fmt_id, content, win32clipboard)
    _write_payloads({fmt_id: encoded})


def write_multi(formats: dict[str, str]) -> None:
    """Replace the clipboard with multiple (mime, content) pairs in ONE
    OpenClipboard transaction.

    Used by `clipboard_copy_markdown` to put `text/html` and `text/plain`
    on the clipboard simultaneously, so paste targets that prefer one
    format over the other (Slack/Gmail vs vim/terminal) each get the
    representation they expect.

    Atomic at the Win32 clipboard chain level: a single Empty -> Set per
    format -> Close transaction. No window during which the clipboard
    holds half the formats.
    """
    if not formats:
        return
    win32clipboard = _import_win32clipboard()

    # Resolve all format IDs and encode all payloads BEFORE the
    # OpenClipboard bracket so encoding errors surface here rather than
    # mid-transaction, where they could leave the clipboard half-written.
    payloads: dict[int, bytes] = {}
    for mime_type, content in formats.items():
        fmt_id = _format_id_for_mime(win32clipboard, mime_type)
        payloads[fmt_id] = _encode_for_format(fmt_id, content, win32clipboard)

    _write_payloads(payloads)


def list_formats() -> list[str]:
    """Enumerate all formats currently on the clipboard.

    Returns the native format names (e.g. "Text", "UnicodeText",
    "HTML Format", "image/svg+xml", "System.String") in the order
    EnumClipboardFormats returned them. The caller in clipboard.py maps
    to MIME via _WIN_TO_MIME and deduplicates Text/UnicodeText collisions.

    Standard format constants get translated to their string names via
    GetClipboardFormatName when possible; some constants (CF_LOCALE,
    CF_OEMTEXT, etc.) have no name in the registered-format table and
    we fall back to the integer-as-string for visibility.
    """
    win32clipboard = _import_win32clipboard()

    _open_clipboard_with_retry(win32clipboard)
    try:
        names: list[str] = []
        fmt = 0
        while True:
            fmt = win32clipboard.EnumClipboardFormats(fmt)
            if fmt == 0:
                break
            name = _format_name(win32clipboard, fmt)
            names.append(name)
    finally:
        win32clipboard.CloseClipboard()
    return names


def _format_name(win32clipboard: Any, fmt: int) -> str:
    """Resolve a clipboard format ID to a human-readable name."""
    # Standard formats: hand-roll the names because GetClipboardFormatName
    # only works for registered (custom) formats, not the CF_* constants.
    standard = {
        win32clipboard.CF_TEXT: "Text",
        win32clipboard.CF_BITMAP: "Bitmap",
        win32clipboard.CF_METAFILEPICT: "MetaFilePict",
        win32clipboard.CF_SYLK: "SYLK",
        win32clipboard.CF_DIF: "DIF",
        win32clipboard.CF_TIFF: "TIFF",
        win32clipboard.CF_OEMTEXT: "OEMText",
        win32clipboard.CF_DIB: "DeviceIndependentBitmap",
        win32clipboard.CF_PALETTE: "Palette",
        win32clipboard.CF_PENDATA: "PenData",
        win32clipboard.CF_RIFF: "RIFF",
        win32clipboard.CF_WAVE: "Wave",
        win32clipboard.CF_UNICODETEXT: "UnicodeText",
        win32clipboard.CF_ENHMETAFILE: "EnhMetaFile",
        win32clipboard.CF_HDROP: "HDrop",
        win32clipboard.CF_LOCALE: "Locale",
        win32clipboard.CF_DIBV5: "DIBv5",
    }
    if fmt in standard:
        return standard[fmt]
    # Registered (custom) formats: look up via the Win32 API.
    try:
        # pywin32 has no type stubs in the configured set; coerce to str.
        name = str(win32clipboard.GetClipboardFormatName(fmt))
        if name:
            return name
    except Exception as exc:
        # GetClipboardFormatName raises for built-in formats it does not
        # know about (CF_LOCALE, CF_OEMTEXT in some pywin32 versions, etc.).
        # Fall through to the numeric stringification but log so a future
        # debugging session can find why a particular format ID didn't
        # resolve to a name.
        logger.debug("GetClipboardFormatName(%d) failed: %s", fmt, exc)
    return f"Format{fmt}"
