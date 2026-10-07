"""Unit tests for the Win32 clipboard wrapper.

Exercises clipboard_win32.read_text / write_text / write_multi / list_formats
with `win32clipboard`, `win32gui`, `win32con` injected as MagicMocks via
sys.modules so the tests run on Linux CI (where pywin32 is not installed).
The raw-SetClipboardData write path drives `kernel32.GlobalAlloc` /
`GlobalLock` / `GlobalUnlock` / `GlobalFree` and `user32.SetClipboardData`
via ctypes; the fixture mocks `ctypes.windll` accordingly. Real Windows-
clipboard round-trips are covered by tests/test_clipboard_win32_integration.py
on the windows-latest runner.

The wrapper is small enough that the unit tests cover every branch:
- _import_win32clipboard cache hit / miss
- _register_format cache hit / miss
- _open_clipboard_with_retry retry budget exhaustion
- _format_id_for_mime for all four supported MIMEs + the unsupported case
- read_text for absent format vs str return vs bytes return
- _encode_for_format for CF_UNICODETEXT (UTF-16 LE + NUL) vs custom (UTF-8)
- _allocate_hglobal GMEM_MOVEABLE allocation, lock/copy/unlock contract,
  failure cleanup
- _write_payloads single + multi format transaction shape, ownership
  transfer to SetClipboardData, GlobalFree cleanup on failure
- write_text and write_multi end-to-end
- list_formats including standard-format-name lookup and custom fallback
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fake_win32clipboard() -> MagicMock:
    """Install MagicMocks for the pywin32 modules our wrapper imports
    lazily (win32clipboard, win32gui, win32con) plus a ctypes.windll
    shape (kernel32 + user32) for the raw HGLOBAL allocation + SetClipboardData
    path. Each is injected via sys.modules so the wrapper's deferred imports
    resolve to the fake; the fakes are removed (or the prior value restored)
    on test teardown.
    """
    fake = MagicMock(name="win32clipboard")
    # Standard format constants so _format_id_for_mime("text/plain") and
    # _format_name's standard-formats dict work without real pywin32.
    fake.CF_TEXT = 1
    fake.CF_BITMAP = 2
    fake.CF_METAFILEPICT = 3
    fake.CF_SYLK = 4
    fake.CF_DIF = 5
    fake.CF_TIFF = 6
    fake.CF_OEMTEXT = 7
    fake.CF_DIB = 8
    fake.CF_PALETTE = 9
    fake.CF_PENDATA = 10
    fake.CF_RIFF = 11
    fake.CF_WAVE = 12
    fake.CF_UNICODETEXT = 13
    fake.CF_ENHMETAFILE = 14
    fake.CF_HDROP = 15
    fake.CF_LOCALE = 16
    fake.CF_DIBV5 = 17

    prior = sys.modules.get("win32clipboard")
    sys.modules["win32clipboard"] = fake

    # _get_clipboard_hwnd creates a process-owned message-only window via
    # win32gui.CreateWindowEx(class="STATIC", parent=HWND_MESSAGE). Stub
    # win32gui and win32con so the tests can run on Linux without pywin32,
    # and assert that OpenClipboard receives the process-owned HWND (not
    # NULL, not the desktop, not anything system-owned). The HWND value
    # 0x10001 is arbitrary; the invariant is "non-NULL and from CreateWindowEx".
    fake_win32gui = MagicMock(name="win32gui")
    fake_win32gui.CreateWindowEx.return_value = 0x10001
    prior_win32gui = sys.modules.get("win32gui")
    sys.modules["win32gui"] = fake_win32gui

    fake_win32con = MagicMock(name="win32con")
    fake_win32con.HWND_MESSAGE = -3  # the actual Win32 constant, for fidelity
    prior_win32con = sys.modules.get("win32con")
    sys.modules["win32con"] = fake_win32con

    # ctypes.windll only exists on Windows; the wrapper's _kernel32 and
    # _user32 helpers access ctypes.windll.kernel32 and ctypes.windll.user32
    # to drive the raw GlobalAlloc + SetClipboardData write path. Inject
    # MagicMocks so the tests can assert on the exact API surface:
    #
    #   kernel32.GlobalAlloc(flags, size) -> integer handle (must be truthy
    #       to indicate success; NULL/0 means allocation failed)
    #   kernel32.GlobalLock(handle) -> integer pointer (the buffer)
    #   kernel32.GlobalUnlock(handle) -> int (we ignore the return)
    #   kernel32.GlobalFree(handle) -> NULL on success, handle on failure
    #   user32.SetClipboardData(format, handle) -> handle on success, NULL on fail
    #
    # Returning a plain int counter so the test can assert distinct handles
    # per allocation; the test increments via side_effect when it needs
    # to validate the allocation-per-format pattern.
    import ctypes as _ctypes

    fake_kernel32 = MagicMock(name="kernel32")
    fake_kernel32.GlobalAlloc.return_value = 0xA1000000  # a non-NULL handle
    fake_kernel32.GlobalLock.return_value = 0xB1000000  # a non-NULL pointer
    fake_kernel32.GlobalUnlock.return_value = 0
    fake_kernel32.GlobalFree.return_value = 0  # NULL = success

    fake_user32 = MagicMock(name="user32")
    # SetClipboardData returns the handle on success; the wrapper only
    # checks truthiness, so any non-zero value works.
    fake_user32.SetClipboardData.return_value = 1

    fake_windll = MagicMock(name="windll")
    fake_windll.kernel32 = fake_kernel32
    fake_windll.user32 = fake_user32
    prior_windll = getattr(_ctypes, "windll", None)
    _ctypes.windll = fake_windll  # type: ignore[attr-defined]

    # The wrapper calls ctypes.memmove(locked_ptr, payload, n) inside
    # _allocate_hglobal. With our fakes, locked_ptr is a plain integer
    # like 0xB1, which is NOT a valid memory address; the real memmove
    # would segfault writing to it. Swap memmove for a recording mock so
    # tests can assert call shape without performing unsafe writes.
    prior_memmove = _ctypes.memmove
    _ctypes.memmove = MagicMock(name="memmove")  # type: ignore[assignment]

    # The real module caches the HWND across calls. Reset between tests so
    # each test starts fresh and CreateWindowEx call counts are deterministic.
    from mcp_clipboard import clipboard_win32 as _mod

    _mod._owner_hwnd = None

    try:
        yield fake
    finally:
        if prior is None:
            sys.modules.pop("win32clipboard", None)
        else:
            sys.modules["win32clipboard"] = prior
        if prior_win32gui is None:
            sys.modules.pop("win32gui", None)
        else:
            sys.modules["win32gui"] = prior_win32gui
        if prior_win32con is None:
            sys.modules.pop("win32con", None)
        else:
            sys.modules["win32con"] = prior_win32con
        _mod._owner_hwnd = None
        if prior_windll is None:
            # ctypes.windll didn't exist before -- delete the attr we
            # added rather than leaving a MagicMock dangling.
            import contextlib

            with contextlib.suppress(AttributeError):
                del _ctypes.windll  # type: ignore[attr-defined]
        else:
            _ctypes.windll = prior_windll  # type: ignore[attr-defined]
        _ctypes.memmove = prior_memmove  # type: ignore[assignment]


@pytest.fixture
def clipboard_win32(fake_win32clipboard):
    """Import the wrapper with the fake win32clipboard already in sys.modules,
    and clear the registered-format cache between tests so RegisterClipboardFormat
    call counts are deterministic."""
    from mcp_clipboard import clipboard_win32 as mod

    mod._format_id_cache.clear()
    return mod


# --- _import_win32clipboard ------------------------------------------------


def test_import_win32clipboard_returns_module_when_available(clipboard_win32, fake_win32clipboard):
    """_import_win32clipboard returns the module from sys.modules when
    pywin32 is installed."""
    assert clipboard_win32._import_win32clipboard() is fake_win32clipboard


def test_import_win32clipboard_raises_clear_error_on_non_windows():
    """When pywin32 is not installed (Linux / macOS) the import raises
    ImportError with a message naming the platform constraint."""
    with patch.dict(sys.modules, {"win32clipboard": None}):
        from mcp_clipboard import clipboard_win32 as mod

        with pytest.raises(ImportError, match="pywin32"):
            mod._import_win32clipboard()


# --- _register_format ------------------------------------------------------


def test_register_format_caches_per_name(clipboard_win32, fake_win32clipboard):
    """_register_format caches the result of RegisterClipboardFormat per
    name so we don't pay the Win32 round-trip more than once per session."""
    fake_win32clipboard.RegisterClipboardFormat.return_value = 0xC123

    fmt_id = clipboard_win32._register_format("HTML Format")
    fmt_id_again = clipboard_win32._register_format("HTML Format")

    assert fmt_id == 0xC123
    assert fmt_id_again == 0xC123
    fake_win32clipboard.RegisterClipboardFormat.assert_called_once_with("HTML Format")


def test_register_format_separate_names_get_separate_ids(clipboard_win32, fake_win32clipboard):
    """Different format names get different cache entries and separate
    RegisterClipboardFormat calls."""
    ids = iter([0xC100, 0xC101, 0xC102])
    fake_win32clipboard.RegisterClipboardFormat.side_effect = lambda _: next(ids)

    a = clipboard_win32._register_format("HTML Format")
    b = clipboard_win32._register_format("Rich Text Format")
    c = clipboard_win32._register_format("image/svg+xml")

    assert {a, b, c} == {0xC100, 0xC101, 0xC102}
    assert fake_win32clipboard.RegisterClipboardFormat.call_count == 3


# --- _open_clipboard_with_retry --------------------------------------------


def test_open_clipboard_with_retry_succeeds_on_first_attempt(clipboard_win32, fake_win32clipboard):
    fake_win32clipboard.OpenClipboard.return_value = None  # success

    clipboard_win32._open_clipboard_with_retry(fake_win32clipboard, retries=3, delay_ms=0)

    fake_win32clipboard.OpenClipboard.assert_called_once()


def test_open_clipboard_passes_process_owned_hwnd(clipboard_win32, fake_win32clipboard):
    """Critical regression guard: OpenClipboard must be called with a process-
    owned HWND created via CreateWindowEx, not NULL and not a system-owned
    window like GetDesktopWindow().

    SetClipboardData requires the calling process to be the clipboard owner.
    EmptyClipboard sets ownership to whatever window was passed to OpenClipboard.
    NULL or system-owned windows leave us not-the-owner and SetClipboardData
    silently fails for registered custom formats -- the bug class that produced
    the SVG silent-no-op symptoms in mc-005 / mc-009 / mc-020 of the early
    PR #146 verification runs.

    The fixture stubs CreateWindowEx to return 0x10001; OpenClipboard MUST
    receive that exact value."""
    fake_win32clipboard.OpenClipboard.return_value = None

    clipboard_win32._open_clipboard_with_retry(fake_win32clipboard, retries=3, delay_ms=0)

    fake_win32clipboard.OpenClipboard.assert_called_once_with(0x10001)


def test_open_clipboard_creates_message_only_window(clipboard_win32, fake_win32clipboard):
    """The HWND we pass to OpenClipboard comes from CreateWindowEx with
    class='STATIC' (built-in USER32 class, no registration needed) and
    parent=HWND_MESSAGE (top-level message-only window, never visible).
    Same pattern pyperclip and Chromium's base::win::MessageWindow use."""
    fake_win32clipboard.OpenClipboard.return_value = None

    clipboard_win32._open_clipboard_with_retry(fake_win32clipboard, retries=3, delay_ms=0)

    fake_win32gui = sys.modules["win32gui"]
    fake_win32con = sys.modules["win32con"]
    fake_win32gui.CreateWindowEx.assert_called_once()
    args = fake_win32gui.CreateWindowEx.call_args[0]
    assert args[1] == "STATIC", f"window class must be 'STATIC', got {args[1]!r}"
    assert args[8] == fake_win32con.HWND_MESSAGE, (
        f"parent must be HWND_MESSAGE for a message-only window, got {args[8]!r}"
    )


def test_open_clipboard_caches_owner_window_across_calls(clipboard_win32, fake_win32clipboard):
    """The owner window is created lazily on first call and cached for the
    process lifetime. Subsequent OpenClipboard calls reuse the same HWND
    rather than spawning a new window each time."""
    fake_win32clipboard.OpenClipboard.return_value = None
    fake_win32gui = sys.modules["win32gui"]

    clipboard_win32._open_clipboard_with_retry(fake_win32clipboard, retries=3, delay_ms=0)
    clipboard_win32._open_clipboard_with_retry(fake_win32clipboard, retries=3, delay_ms=0)
    clipboard_win32._open_clipboard_with_retry(fake_win32clipboard, retries=3, delay_ms=0)

    assert fake_win32gui.CreateWindowEx.call_count == 1
    assert fake_win32clipboard.OpenClipboard.call_count == 3
    for call in fake_win32clipboard.OpenClipboard.call_args_list:
        assert call.args == (0x10001,)


def test_get_clipboard_hwnd_inner_lock_returns_cached_hwnd(clipboard_win32, fake_win32clipboard):
    """Double-checked locking has TWO None-checks: one before the lock
    and one inside. Swap the module-level lock for a fake whose __enter__
    pre-populates _owner_hwnd, simulating the race: the inner None-check
    then returns the cached HWND without falling into CreateWindowEx."""
    fake_win32gui = sys.modules["win32gui"]
    sentinel_hwnd = 0xDEADBEEF

    class _RacingLock:
        def __enter__(self) -> _RacingLock:
            clipboard_win32._owner_hwnd = sentinel_hwnd
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    with patch.object(clipboard_win32, "_owner_hwnd_lock", _RacingLock()):
        result = clipboard_win32._get_clipboard_hwnd()

    assert result == sentinel_hwnd
    fake_win32gui.CreateWindowEx.assert_not_called()


def test_get_clipboard_hwnd_is_thread_safe(clipboard_win32, fake_win32clipboard):
    """Concurrent first-callers must not leak HWNDs."""
    import threading as _threading

    fake_win32gui = sys.modules["win32gui"]

    n_threads = 32
    barrier = _threading.Barrier(n_threads)
    hwnds: list[int] = []
    hwnds_lock = _threading.Lock()

    def race():
        barrier.wait()
        h = clipboard_win32._get_clipboard_hwnd()
        with hwnds_lock:
            hwnds.append(h)

    threads = [_threading.Thread(target=race) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert fake_win32gui.CreateWindowEx.call_count == 1
    assert len(hwnds) == n_threads
    assert set(hwnds) == {0x10001}


def test_open_clipboard_with_retry_succeeds_after_transient_failure(
    clipboard_win32, fake_win32clipboard
):
    """Two failures, then success."""
    attempts = iter([RuntimeError("locked"), RuntimeError("locked"), None])

    def open_side_effect(_hwnd):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    fake_win32clipboard.OpenClipboard.side_effect = open_side_effect

    clipboard_win32._open_clipboard_with_retry(fake_win32clipboard, retries=5, delay_ms=0)

    assert fake_win32clipboard.OpenClipboard.call_count == 3


def test_open_clipboard_with_retry_raises_after_budget_exhausted(
    clipboard_win32, fake_win32clipboard
):
    """If every retry fails, RuntimeError surfaces with the last error."""
    fake_win32clipboard.OpenClipboard.side_effect = RuntimeError("locked")

    with pytest.raises(RuntimeError, match="OpenClipboard failed after 3 retries"):
        clipboard_win32._open_clipboard_with_retry(fake_win32clipboard, retries=3, delay_ms=0)

    assert fake_win32clipboard.OpenClipboard.call_count == 3


# --- _format_id_for_mime ---------------------------------------------------


def test_format_id_for_mime_text_plain_uses_cf_unicodetext(clipboard_win32, fake_win32clipboard):
    fmt_id = clipboard_win32._format_id_for_mime(fake_win32clipboard, "text/plain")
    assert fmt_id == fake_win32clipboard.CF_UNICODETEXT
    fake_win32clipboard.RegisterClipboardFormat.assert_not_called()


@pytest.mark.parametrize(
    "mime_type,expected_name",
    [
        ("text/html", "HTML Format"),
        ("text/rtf", "Rich Text Format"),
        ("image/svg+xml", "image/svg+xml"),
    ],
)
def test_format_id_for_mime_registered_formats(
    clipboard_win32, fake_win32clipboard, mime_type, expected_name
):
    fake_win32clipboard.RegisterClipboardFormat.return_value = 0xC200
    clipboard_win32._format_id_for_mime(fake_win32clipboard, mime_type)
    fake_win32clipboard.RegisterClipboardFormat.assert_called_once_with(expected_name)


def test_format_id_for_mime_unsupported_raises(clipboard_win32, fake_win32clipboard):
    with pytest.raises(ValueError, match="Unsupported MIME type"):
        clipboard_win32._format_id_for_mime(fake_win32clipboard, "application/octet-stream")


# --- read_text -------------------------------------------------------------


def test_read_text_returns_empty_string_when_format_absent(clipboard_win32, fake_win32clipboard):
    fake_win32clipboard.IsClipboardFormatAvailable.return_value = False

    result = clipboard_win32.read_text("text/plain")

    assert result == ""
    fake_win32clipboard.OpenClipboard.assert_called_once()
    fake_win32clipboard.CloseClipboard.assert_called_once()
    fake_win32clipboard.GetClipboardData.assert_not_called()


def test_read_text_returns_str_for_unicodetext(clipboard_win32, fake_win32clipboard):
    fake_win32clipboard.IsClipboardFormatAvailable.return_value = True
    fake_win32clipboard.GetClipboardData.return_value = "hello — world"

    result = clipboard_win32.read_text("text/plain")

    assert result == "hello — world"


def test_read_text_decodes_utf8_bytes_for_registered_formats(clipboard_win32, fake_win32clipboard):
    fake_win32clipboard.IsClipboardFormatAvailable.return_value = True
    fake_win32clipboard.RegisterClipboardFormat.return_value = 0xC300
    fake_win32clipboard.GetClipboardData.return_value = b"<svg>\xe2\x80\x94</svg>"

    result = clipboard_win32.read_text("image/svg+xml")

    assert result == "<svg>—</svg>"


def test_read_text_strips_trailing_nul_byte_from_registered_format(
    clipboard_win32, fake_win32clipboard
):
    """When the writer was a .NET / OLE-backed app, the registered-format
    bytes come back NUL-terminated (the OLE STGMEDIUM.set path
    GlobalAlloc(len + 1)s and writes a trailing NUL). read_text strips
    that NUL so callers always see the exact source bytes regardless of
    who wrote the clipboard. Our own write path no longer adds the NUL
    (raw SetClipboardData with GlobalAlloc(len)), but inter-op with
    other writers is still in scope."""
    fake_win32clipboard.IsClipboardFormatAvailable.return_value = True
    fake_win32clipboard.RegisterClipboardFormat.return_value = 0xC301
    fake_win32clipboard.GetClipboardData.return_value = b"<svg>x</svg>\x00"

    result = clipboard_win32.read_text("image/svg+xml")

    assert result == "<svg>x</svg>"
    fake_win32clipboard.GetClipboardData.return_value = b"<svg>y</svg>"
    assert clipboard_win32.read_text("image/svg+xml") == "<svg>y</svg>"


def test_read_text_closes_clipboard_on_exception(clipboard_win32, fake_win32clipboard):
    fake_win32clipboard.IsClipboardFormatAvailable.return_value = True
    fake_win32clipboard.GetClipboardData.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        clipboard_win32.read_text("text/plain")

    fake_win32clipboard.CloseClipboard.assert_called_once()


def test_read_text_str_fallback_for_unexpected_return_type(clipboard_win32, fake_win32clipboard):
    fake_win32clipboard.IsClipboardFormatAvailable.return_value = True
    fake_win32clipboard.RegisterClipboardFormat.return_value = 0xC700

    class _UnexpectedShape:
        def __str__(self) -> str:
            return "fallback-str"

    fake_win32clipboard.GetClipboardData.return_value = _UnexpectedShape()

    result = clipboard_win32.read_text("image/svg+xml")

    assert result == "fallback-str"


# --- _encode_for_format ----------------------------------------------------


def test_encode_for_format_cf_unicodetext_is_utf16le_null_terminated(
    clipboard_win32, fake_win32clipboard
):
    """CF_UNICODETEXT requires null-terminated UTF-16 LE per MSDN.
    Encoding it ourselves rather than going through pywin32's str
    overload makes the byte layout explicit at the test level."""
    encoded = clipboard_win32._encode_for_format(
        fake_win32clipboard.CF_UNICODETEXT, "hi —", fake_win32clipboard
    )

    assert encoded == "hi —".encode("utf-16-le") + b"\x00\x00"


def test_encode_for_format_custom_format_is_utf8(clipboard_win32, fake_win32clipboard):
    """Registered custom formats (HTML Format, Rich Text Format,
    image/svg+xml) take raw UTF-8 bytes. No null terminator."""
    encoded = clipboard_win32._encode_for_format(0xC100, "<svg>—</svg>", fake_win32clipboard)

    assert encoded == "<svg>—</svg>".encode()


# --- _allocate_hglobal -----------------------------------------------------


def test_allocate_hglobal_uses_gmem_moveable(clipboard_win32, fake_win32clipboard):
    """GlobalAlloc MUST be called with GMEM_MOVEABLE (0x0002). Per MSDN
    SetClipboardData, GMEM_FIXED handles are silently rejected -- the
    documented cause of the silent-no-op symptom class. This is the
    canary that fails if someone "simplifies" the code by accidentally
    dropping the flag."""
    import ctypes as _ctypes

    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.return_value = 0xA1
    kernel32.GlobalLock.return_value = 0xB1

    clipboard_win32._allocate_hglobal(b"abc")

    kernel32.GlobalAlloc.assert_called_once()
    flags = kernel32.GlobalAlloc.call_args.args[0]
    assert flags == 0x0002, f"GlobalAlloc flag must be GMEM_MOVEABLE (0x0002); got {flags:#x}"


def test_allocate_hglobal_locks_copies_unlocks_in_order(clipboard_win32, fake_win32clipboard):
    """The canonical lock/copy/unlock dance: GlobalLock the handle to get
    a writable pointer, memcpy the payload, GlobalUnlock to release the
    lock count. SetClipboardData then walks the locked region of the
    handle. If we forget to Unlock, the handle stays pinned and the OS
    cannot move it -- not a correctness bug but a documented antipattern."""
    import ctypes as _ctypes

    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.return_value = 0xCAFE
    kernel32.GlobalLock.return_value = 0xBEEF

    call_order: list[str] = []
    kernel32.GlobalAlloc.side_effect = lambda *_a, **_kw: call_order.append("alloc") or 0xCAFE
    kernel32.GlobalLock.side_effect = lambda *_a, **_kw: call_order.append("lock") or 0xBEEF
    kernel32.GlobalUnlock.side_effect = lambda *_a, **_kw: call_order.append("unlock") or 0

    handle = clipboard_win32._allocate_hglobal(b"payload")

    assert call_order == ["alloc", "lock", "unlock"]
    assert handle == 0xCAFE
    kernel32.GlobalLock.assert_called_once_with(0xCAFE)
    kernel32.GlobalUnlock.assert_called_once_with(0xCAFE)


def test_allocate_hglobal_passes_exact_payload_size(clipboard_win32, fake_win32clipboard):
    """GlobalAlloc receives the payload length exactly -- no NUL terminator,
    no padding, no rounding. Our reads strip trailing NULs from interop
    payloads so we must not add one on our writes."""
    import ctypes as _ctypes

    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.return_value = 0xA1
    kernel32.GlobalLock.return_value = 0xB1

    clipboard_win32._allocate_hglobal(b"<svg/>")

    size = kernel32.GlobalAlloc.call_args.args[1]
    assert size == len(b"<svg/>")


def test_allocate_hglobal_empty_payload_uses_size_one(clipboard_win32, fake_win32clipboard):
    """GlobalAlloc(0) is technically defined but yields a handle that
    SetClipboardData cannot meaningfully consume. Round up to 1 so the
    OS always has a real byte to point at; consumers that read an empty
    payload won't ever inspect the byte."""
    import ctypes as _ctypes

    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.return_value = 0xA1
    kernel32.GlobalLock.return_value = 0xB1

    clipboard_win32._allocate_hglobal(b"")

    assert kernel32.GlobalAlloc.call_args.args[1] == 1


def test_allocate_hglobal_raises_memoryerror_on_alloc_failure(clipboard_win32, fake_win32clipboard):
    """GlobalAlloc returns NULL on out-of-global-memory. We must raise so
    the upstream MCP layer surfaces the failure rather than handing a
    NULL handle to SetClipboardData."""
    import ctypes as _ctypes

    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.return_value = 0  # NULL = failure

    with pytest.raises(MemoryError, match="GlobalAlloc"):
        clipboard_win32._allocate_hglobal(b"x")


def test_allocate_hglobal_frees_handle_on_lock_failure(clipboard_win32, fake_win32clipboard):
    """If GlobalLock fails (defensive; should not happen for a fresh
    GMEM_MOVEABLE handle), we still own the GlobalAlloc'd handle and
    must GlobalFree it before raising. Otherwise we leak global memory
    per failed allocation."""
    import ctypes as _ctypes

    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.return_value = 0xA1
    kernel32.GlobalLock.return_value = 0  # NULL = failure

    with pytest.raises(OSError, match="GlobalLock"):
        clipboard_win32._allocate_hglobal(b"x")

    kernel32.GlobalFree.assert_called_once_with(0xA1)


# --- _write_payloads (raw SetClipboardData transaction) -------------------


def test_write_payloads_single_format_full_transaction(clipboard_win32, fake_win32clipboard):
    """Happy path for one format: OpenClipboard, EmptyClipboard,
    GlobalAlloc + lock/copy/unlock, SetClipboardData, CloseClipboard.
    No retry, no verify, no pump -- the canonical Win32 sequence
    matching Chromium / Qt / clipboard-win."""
    import ctypes as _ctypes

    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32 = _ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.return_value = 0xCAFE
    kernel32.GlobalLock.return_value = 0xBEEF
    user32.SetClipboardData.return_value = 0xCAFE  # success

    clipboard_win32._write_payloads({0xC100: b"<svg/>"})

    fake_win32clipboard.OpenClipboard.assert_called_once()
    fake_win32clipboard.EmptyClipboard.assert_called_once()
    user32.SetClipboardData.assert_called_once()
    fmt_arg, handle_arg = user32.SetClipboardData.call_args.args
    assert fmt_arg.value == 0xC100
    assert handle_arg.value == 0xCAFE
    fake_win32clipboard.CloseClipboard.assert_called_once()
    # No post-write verify pair.
    fake_win32clipboard.IsClipboardFormatAvailable.assert_not_called()


def test_write_payloads_multi_format_uses_one_transaction(clipboard_win32, fake_win32clipboard):
    """A multi-format write is ONE OpenClipboard / EmptyClipboard /
    multiple-SetClipboardData / CloseClipboard transaction. EmptyClipboard
    runs exactly once -- not once per format -- so the formats appear on
    the clipboard atomically."""
    import ctypes as _ctypes

    handles = iter([0xCAFE0001, 0xCAFE0002])
    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32 = _ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.side_effect = lambda *_a, **_kw: next(handles)
    kernel32.GlobalLock.return_value = 0xBEEF
    user32.SetClipboardData.return_value = 1  # success

    clipboard_win32._write_payloads({13: b"x\x00", 0xC100: b"<p>x</p>"})

    fake_win32clipboard.OpenClipboard.assert_called_once()
    fake_win32clipboard.EmptyClipboard.assert_called_once()
    assert user32.SetClipboardData.call_count == 2
    fake_win32clipboard.CloseClipboard.assert_called_once()
    fake_win32clipboard.IsClipboardFormatAvailable.assert_not_called()


def test_write_payloads_empty_input_is_noop(clipboard_win32, fake_win32clipboard):
    """Direct call: an empty payloads dict short-circuits before any
    clipboard / allocation work. (write_multi({}) has its own early
    return upstream that doesn't reach _write_payloads.)"""
    import ctypes as _ctypes

    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32 = _ctypes.windll.user32  # type: ignore[attr-defined]

    clipboard_win32._write_payloads({})

    fake_win32clipboard.OpenClipboard.assert_not_called()
    fake_win32clipboard.EmptyClipboard.assert_not_called()
    kernel32.GlobalAlloc.assert_not_called()
    user32.SetClipboardData.assert_not_called()


def test_write_payloads_closes_clipboard_on_setclipboarddata_failure(
    clipboard_win32, fake_win32clipboard
):
    """If SetClipboardData returns NULL (failure) mid-transaction, the
    OSError surfaces AFTER CloseClipboard has run so the clipboard does
    not stay locked. The leftover handles must also be GlobalFree'd."""
    import ctypes as _ctypes

    handles = iter([0xCAFE0001, 0xCAFE0002])
    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32 = _ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.side_effect = lambda *_a, **_kw: next(handles)
    kernel32.GlobalLock.return_value = 0xBEEF
    # First SetClipboardData succeeds, second fails (returns NULL).
    user32.SetClipboardData.side_effect = [1, 0]

    with pytest.raises(OSError, match="SetClipboardData"):
        clipboard_win32._write_payloads({13: b"x\x00", 0xC100: b"<p>x</p>"})

    fake_win32clipboard.CloseClipboard.assert_called_once()
    # The second handle (which SetClipboardData rejected) must be freed.
    # The first handle was transferred to the system and is NOT freed.
    freed = [c.args[0].value for c in kernel32.GlobalFree.call_args_list]
    assert 0xCAFE0002 in freed
    assert 0xCAFE0001 not in freed


def test_write_payloads_frees_handles_when_open_clipboard_exhausts(
    clipboard_win32, fake_win32clipboard
):
    """If OpenClipboard exhausts its retry budget AFTER we have already
    allocated HGLOBALs for the payloads, those handles must be
    GlobalFree'd. Otherwise we leak global memory per write-failure
    burst."""
    import ctypes as _ctypes

    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.return_value = 0xCAFE
    kernel32.GlobalLock.return_value = 0xBEEF
    fake_win32clipboard.OpenClipboard.side_effect = RuntimeError("contended")

    # _open_clipboard_with_retry sleeps between attempts; cap the retry
    # budget to 1 to keep the test fast.
    with patch.object(clipboard_win32, "_open_clipboard_with_retry") as mocked_open:
        mocked_open.side_effect = RuntimeError("OpenClipboard failed")
        with pytest.raises(RuntimeError, match="OpenClipboard failed"):
            clipboard_win32._write_payloads({0xC100: b"<svg/>"})

    kernel32.GlobalFree.assert_called_once()


def test_write_payloads_no_post_write_verify(clipboard_win32, fake_win32clipboard):
    """Architectural invariant: there is NO post-write
    IsClipboardFormatAvailable verify. This matches Chromium, Qt,
    clipboard-win, pyperclip, pyclip, and .NET WinForms (whose 10x100ms
    retry is on the OpenClipboard side, not on post-write verify).
    A prior revision (commit 75bcb0f) added a verify-retry tax;
    QEMU testing showed it made the race STRICTLY WORSE under active
    chain observers (race-bucket went 4/6 to 0/6 PASS) because each
    verify pair generates an additional WM_CLIPBOARDUPDATE
    notification that amplifies observer contention. The verify-retry
    is gone. If a future change reintroduces it, this test fails the
    architectural smell."""
    import ctypes as _ctypes

    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32 = _ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.return_value = 0xCAFE
    kernel32.GlobalLock.return_value = 0xBEEF
    user32.SetClipboardData.return_value = 1

    clipboard_win32._write_payloads({0xC100: b"<svg/>"})

    # OpenClipboard fires ONCE (just the write transaction).
    fake_win32clipboard.OpenClipboard.assert_called_once()
    fake_win32clipboard.CloseClipboard.assert_called_once()
    # SetClipboardData fires ONCE.
    user32.SetClipboardData.assert_called_once()
    # IsClipboardFormatAvailable is NEVER called on the write path.
    fake_win32clipboard.IsClipboardFormatAvailable.assert_not_called()


def test_write_payloads_no_seq_number_sampling(clipboard_win32, fake_win32clipboard):
    """Architectural invariant: the write path must NOT call
    GetClipboardSequenceNumber. A prior revision (commit ab05aef)
    added a passive seq-number canary bracketing the transaction as
    an observation-only diagnostic. QEMU re-test with chain observers
    DISABLED showed the canary itself measurably degraded the race
    posture: 0/6 race-bucket PASS vs the canary-free 6/6 baseline on
    the same observer setting. Even "passive" instrumentation on the
    write hot path is not free on real Windows 11. The canary is
    removed. If a future change reintroduces sampling at the write
    boundary, this test fails the architectural smell."""
    import ctypes as _ctypes

    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32 = _ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.return_value = 0xCAFE
    kernel32.GlobalLock.return_value = 0xBEEF
    user32.SetClipboardData.return_value = 1

    clipboard_win32._write_payloads({0xC100: b"<svg/>"})

    user32.GetClipboardSequenceNumber.assert_not_called()


# --- write_text ------------------------------------------------------------


def test_write_text_plain_encodes_utf16_le_null_terminated(clipboard_win32, fake_win32clipboard):
    """text/plain encodes to UTF-16 LE + NUL (CF_UNICODETEXT format
    contract) and gets handed to _write_payloads keyed on
    CF_UNICODETEXT."""
    import ctypes as _ctypes

    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32 = _ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.return_value = 0xA1
    kernel32.GlobalLock.return_value = 0xB1
    user32.SetClipboardData.return_value = 0xA1

    captured: dict[int, bytes] = {}

    def capture_payload(_flags: int, size: int) -> int:
        # Stash the payload during the lock/copy/unlock dance by patching
        # ctypes.memmove. Simpler: capture the size and read it back from
        # the call args of GlobalAlloc.
        captured["size"] = size
        return 0xA1

    kernel32.GlobalAlloc.side_effect = capture_payload

    clipboard_win32.write_text("hello", "text/plain")

    # UTF-16 LE: 5 wchars + NUL = 12 bytes.
    assert captured["size"] == len("hello".encode("utf-16-le") + b"\x00\x00")
    fmt_arg, _ = user32.SetClipboardData.call_args.args
    assert fmt_arg.value == fake_win32clipboard.CF_UNICODETEXT


def test_write_text_registered_format_encodes_utf8(clipboard_win32, fake_win32clipboard):
    """text/html / text/rtf / image/svg+xml encode to UTF-8 and land
    under their registered format ID."""
    import ctypes as _ctypes

    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32 = _ctypes.windll.user32  # type: ignore[attr-defined]
    fake_win32clipboard.RegisterClipboardFormat.return_value = 0xC400
    kernel32.GlobalAlloc.return_value = 0xA2
    kernel32.GlobalLock.return_value = 0xB2
    user32.SetClipboardData.return_value = 0xA2

    clipboard_win32.write_text("<svg>—</svg>", "image/svg+xml")

    # UTF-8: 7 ASCII + 3 (em dash) + 6 ASCII = 16 bytes; no NUL terminator.
    expected_size = len("<svg>—</svg>".encode())
    size_arg = kernel32.GlobalAlloc.call_args.args[1]
    assert size_arg == expected_size
    fmt_arg, _ = user32.SetClipboardData.call_args.args
    assert fmt_arg.value == 0xC400


def test_write_text_uses_open_empty_set_close_sequence(clipboard_win32, fake_win32clipboard):
    """Operation order: OpenClipboard -> EmptyClipboard -> SetClipboardData
    -> CloseClipboard. EmptyClipboard MUST come AFTER OpenClipboard
    (the open assigns ownership; empty propagates it) and BEFORE
    SetClipboardData (we have to own the clipboard before writing
    registered formats)."""
    import ctypes as _ctypes

    user32 = _ctypes.windll.user32  # type: ignore[attr-defined]

    call_order: list[str] = []
    fake_win32clipboard.OpenClipboard.side_effect = lambda _h: call_order.append("open")
    fake_win32clipboard.EmptyClipboard.side_effect = lambda: call_order.append("empty")
    user32.SetClipboardData.side_effect = lambda *_a: (call_order.append("set"), 1)[1]
    fake_win32clipboard.CloseClipboard.side_effect = lambda: call_order.append("close")

    clipboard_win32.write_text("hi", "text/plain")

    assert call_order == ["open", "empty", "set", "close"]


# --- write_multi -----------------------------------------------------------


def test_write_multi_empty_input_is_noop(clipboard_win32, fake_win32clipboard):
    import ctypes as _ctypes

    user32 = _ctypes.windll.user32  # type: ignore[attr-defined]

    clipboard_win32.write_multi({})

    fake_win32clipboard.OpenClipboard.assert_not_called()
    user32.SetClipboardData.assert_not_called()


def test_write_multi_publishes_all_formats_in_one_transaction(clipboard_win32, fake_win32clipboard):
    """Multi-format write: one Open/Empty/Set..Set/Close transaction.
    All formats land atomically; no intermediate state where the
    clipboard holds only some of them."""
    import ctypes as _ctypes

    handles = iter([0xC0FFEE01, 0xC0FFEE02])
    kernel32 = _ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32 = _ctypes.windll.user32  # type: ignore[attr-defined]
    fake_win32clipboard.RegisterClipboardFormat.return_value = 0xC500
    kernel32.GlobalAlloc.side_effect = lambda *_a, **_kw: next(handles)
    kernel32.GlobalLock.return_value = 0xB1
    user32.SetClipboardData.return_value = 1

    clipboard_win32.write_multi({"text/plain": "hi", "text/html": "<p>hi</p>"})

    fake_win32clipboard.OpenClipboard.assert_called_once()
    fake_win32clipboard.EmptyClipboard.assert_called_once()
    assert user32.SetClipboardData.call_count == 2
    fake_win32clipboard.CloseClipboard.assert_called_once()


def test_write_multi_resolves_formats_before_clipboard_open(clipboard_win32, fake_win32clipboard):
    """Format resolution (RegisterClipboardFormat) and payload encoding
    must happen BEFORE OpenClipboard so encoding errors raise outside
    the transaction bracket -- otherwise the clipboard could be left
    in an Open state with no Close (since RegisterClipboardFormat
    raising would skip the try/finally below it)."""
    call_order: list[str] = []
    fake_win32clipboard.RegisterClipboardFormat.side_effect = lambda _name: (
        call_order.append("register"),
        0xC500,
    )[1]
    fake_win32clipboard.OpenClipboard.side_effect = lambda _h: call_order.append("open")

    clipboard_win32.write_multi({"text/html": "<p>x</p>"})

    assert call_order.index("register") < call_order.index("open")


# --- list_formats ----------------------------------------------------------


def test_list_formats_iterates_via_enumclipboardformats(clipboard_win32, fake_win32clipboard):
    enum_returns = iter([13, 0xC700, 0])
    fake_win32clipboard.EnumClipboardFormats.side_effect = lambda _prev: next(enum_returns)
    fake_win32clipboard.GetClipboardFormatName.return_value = "image/svg+xml"

    result = clipboard_win32.list_formats()

    assert result == ["UnicodeText", "image/svg+xml"]
    fake_win32clipboard.OpenClipboard.assert_called_once()
    fake_win32clipboard.CloseClipboard.assert_called_once()


def test_list_formats_falls_back_to_numeric_for_unknown_constant(
    clipboard_win32, fake_win32clipboard
):
    enum_returns = iter([0x9999, 0])
    fake_win32clipboard.EnumClipboardFormats.side_effect = lambda _prev: next(enum_returns)
    fake_win32clipboard.GetClipboardFormatName.side_effect = RuntimeError("no name")

    result = clipboard_win32.list_formats()

    assert result == [f"Format{0x9999}"]


def test_list_formats_closes_clipboard_on_exception(clipboard_win32, fake_win32clipboard):
    fake_win32clipboard.EnumClipboardFormats.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        clipboard_win32.list_formats()

    fake_win32clipboard.CloseClipboard.assert_called_once()
