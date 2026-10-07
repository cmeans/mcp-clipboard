# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

mcp-clipboard is an MCP (Model Context Protocol) server that reads and writes the system clipboard — tables, plain text, code, JSON, URLs, images, and more. Preserves structure when possible (e.g. spreadsheet row/column layout from HTML) and returns non-tabular content with smart formatting. Images on the clipboard are returned as viewable content; audio and video are detected and reported but not returned.

## Commands

```bash
# Install dependencies
uv sync

# Run all tests (mocked, no clipboard needed)
uv run pytest

# Run a single test file
uv run pytest tests/test_parser.py

# Run a single test function
uv run pytest tests/test_parser.py::test_parse_google_sheets_html

# Run integration tests (requires real clipboard daemon)
uv run pytest -m integration

# Run the MCP server (stdio mode)
uv run mcp-clipboard

# Run with debug logging
uv run mcp-clipboard --debug
# or: MCP_CLIPBOARD_DEBUG=1 uv run mcp-clipboard

# Test interactively with MCP Inspector
uv run mcp dev src/mcp_clipboard/server.py

# Build wheel (to verify packaging)
uv build --wheel

# Publish to PyPI (do NOT publish directly — push a tag to trigger GitHub Actions)
git tag v0.1.x && git push origin v0.1.x   # triggers .github/workflows/publish.yml
git tag test-v0.1.x && git push origin test-v0.1.x  # triggers test-publish.yml (TestPyPI)
```

## Architecture

Three-layer design with clean separation:

- **server.py** — MCP server (`FastMCP`, name `mcp_clipboard`) exposing 7 tools:
  - `clipboard_paste(output_format, include_schema, selection)` — Primary tool. Handles any clipboard content: tables → markdown/json/csv; non-tabular → smart formatting (JSON, code, URL, text). Images are returned as base64-encoded image content. Audio/video are detected and reported. `selection` defaults to `"clipboard"`; pass `"primary"` on X11/Wayland to read the middle-click/select-text-to-paste buffer.
  - `clipboard_copy(content)` — Writes text to the system clipboard.
  - `clipboard_copy_markdown(text)` — Renders markdown to HTML and places both formats on the clipboard so paste targets pick the right one. macOS/Windows write both atomically via multi-format clipboard APIs; Wayland/X11 are single-MIME and write only `text/html`.
  - `clipboard_copy_image(image_data, mime_type)` — Writes a PNG or JPEG image to the system clipboard from base64-encoded bytes. Pass-through, no re-encoding. Magic bytes are validated against the declared MIME.
  - `clipboard_read_raw(mime_type, selection)` — Returns raw clipboard content for a given MIME type (truncated at 50KB). Rejects binary MIME types. Accepts `selection="primary"` on X11/Wayland.
  - `clipboard_list_formats(selection)` — Lists available MIME types on clipboard. Accepts `selection="primary"` on X11/Wayland.
  - `clipboard_version()` — Returns the running mcp-clipboard package version as `{"name": "mcp-clipboard", "version": "<x.y.z>"}`. Diagnostic surface for hosts that don't expose the standard MCP `serverInfo` block to the model, and for test harnesses that need to record which build served a given run.

- **clipboard.py** — Platform-agnostic clipboard abstraction. Auto-detects backend (Wayland `wl-paste`/`wl-copy`, X11 `xclip`, macOS `osascript`/`pbpaste`/`pbcopy`, Windows PowerShell). All operations are async with 5-second timeout. Exit code 1 means "format not available" (not an error). macOS UTI types and Windows format names are mapped to MIME types in `list_formats`. Supports text read/write and binary image reads.

- **parser.py** — HTML table extraction (custom `HTMLParser` subclass), TSV parsing, HTML-to-text extraction, content-type detection (json/url/code/text), and table formatters (Markdown, JSON, CSV).

- **instructions/** — Markdown files containing MCP server instructions and tool descriptions. Loaded at startup by `server.py` via `_load_instruction()`. These are the descriptions the host model sees when deciding which tool to call. Edit these files to change tool behavior descriptions without touching Python code.

**Data flow**: `clipboard_paste` → try HTML → `parse_html_table()` → if rows, format table → return. Else try plain text → `parse_tsv()` → if rows, format table → return. Else if no text content, check for image formats → read and return as image content. Else check for other binary formats → report. Else `detect_content_type()` → return with smart formatting.

## Testing

Tests use `pytest` with `pytest-asyncio` (async mode: auto). All pytest config is in `pyproject.toml` (`[tool.pytest.ini_options]`). Server tests mock clipboard operations via `unittest.mock.patch` on `mcp_clipboard.server.clipboard`. Parser tests are pure unit tests with no mocking needed. Platform-specific backend tests (macOS, Windows) mock `mcp_clipboard.clipboard._run`.

## Key Details

- Python 3.11+ required
- Runtime dependencies: `mcp[cli]>=1.15.0,<2` (floor: `mcp.types.Icon` first ships in 1.15.0; cap: mcp 2.x renamed `FastMCP`, migration tracked in #157), `markdown-it-py>=3.0` (for `clipboard_copy_markdown` rendering)
- HTML parsing uses stdlib `html.parser` (no BeautifulSoup)
- Pytest config is in `pyproject.toml` (no separate pytest.ini)
- Entry point: `mcp_clipboard.server:main()`
- Tool descriptions live in `src/mcp_clipboard/instructions/*.md` — edit those files to change what the host model sees
- `pyproject.toml` `artifacts` setting ensures instruction `.md` files are included in the wheel
- PyPI package name, command, and import are all `mcp-clipboard` / `mcp_clipboard`
- Linux with Wayland is tested on real hardware. Windows has been exercised end-to-end on a QEMU Windows guest as of v2.5.x (#129, the UTF-8 stdin encoding bug, was found and fixed via that testing). X11 has unit tests but is unverified on live hardware. macOS is complete with unit tests but has not been exercised on real hardware.
- `clipboard_paste` intentionally has no return type annotation — adding `-> str | Image` causes FastMCP to fail Pydantic schema generation for `Image`

## Conventions

### Adding a CHANGELOG entry on every PR

Every PR — features, fixes, infra, tests, docs — adds an entry to `CHANGELOG.md` under the `## [Unreleased]` section at the top of the file. Do not defer CHANGELOG updates until release prep.

`CHANGELOG.md` follows **[Keep a Changelog](https://keepachangelog.com/) categories**:
- `### Added` — anything new: features, capabilities, tests, docs, dev tooling
- `### Changed` — behavior or API changes that aren't bug fixes
- `### Fixed` — bug fixes

Reference the PR number and any closed issue: `- ... (#16) — closes #14`. If no `## [Unreleased]` section exists (because the previous release just shipped), add one above the latest version section.

Dependabot PRs are exempt from manual entry — `.github/workflows/dependabot-changelog.yml` auto-prepends an entry under `### Changed` (or creates the subsection at the right Keep-a-Changelog position if it does not exist). The workflow needs `BOT_APP_ID` and `BOT_APP_PRIVATE_KEY` repo secrets to mint a GitHub App installation token; without those secrets the workflow fails fast on the App-token mint step.

## Packaging Feature Branches

- **`feature/homebrew-tap`** (local only) — Homebrew formula, update script, and CI template for a `cmeans/homebrew-mcp-clipboard` tap. Formula resource stanzas need populating via `brew update-python-resources` on macOS before the tap can be published. Has 36 transitive dependencies from `mcp[cli]`.
- **`feature/aur-package`** (pushed to origin) — PKGBUILD for Arch Linux AUR. Ready to test in an Arch VM or Docker container. Note: depends on `python-mcp` which may need its own AUR package first. Test with `makepkg -si` in a VM or Docker build validation per `packaging/aur/README.md`.
