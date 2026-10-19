# Testing Guide

This checklist is for contributors validating mcp-clipboard with real MCP
clients and real OS clipboards. It complements the automated unit tests in
`tests/`; it does not replace them.

## Quick Triage

Start each report with this small environment block:

```text
OS:
Desktop/session:
MCP client:
mcp-clipboard version:
Install runner:
Clipboard backend:
```

Useful commands:

```bash
uvx mcp-clipboard --check
uvx mcp-clipboard --version
```

If testing from a checkout:

```bash
uv sync --extra dev
uv run pytest
uv run pytest -m integration
```

The integration tests touch the real clipboard and may be skipped when a
clipboard daemon or platform helper is unavailable.

## Manual MCP Session

Register the server with your MCP client, then run the checks below in a normal
chat. Record the exact prompt, whether the expected tool was called, and the
observed output.

| Check | Prompt | Expected result |
| --- | --- | --- |
| Plain text read | Copy `hello clipboard`, then ask "what is on my clipboard?" | `clipboard_paste` returns the same text. |
| Plain text write | Ask "copy `hello from MCP` to my clipboard" | Pasting into another app yields exactly that text. |
| Long command write | Ask the client to copy a long shell command with pipes, quotes, and flags | The pasted command has no terminal padding, inserted hard wraps, or trailing spaces. |
| Code block read | Copy a small function, then ask "read the code I copied" | The result preserves line breaks and indentation. |
| URL read | Copy a URL, then ask "what URL did I copy?" | The URL is returned without added markup or whitespace. |
| Markdown write | Ask to copy a short markdown list using `clipboard_copy_markdown` | Rich-text targets receive formatted HTML; plain-text targets receive usable markdown/source. |
| Image read | Copy a PNG/JPEG image, then ask "describe the image on my clipboard" | The client receives image content, not a text fallback. |
| Format list | Ask "what clipboard formats are available?" | `clipboard_list_formats` returns useful MIME/format names for the platform. |

## Platform-Specific Checks

### Linux Wayland

- Confirm `wl-copy` and `wl-paste` are installed.
- Run `uvx mcp-clipboard --check` and confirm the detected backend is Wayland.
- Test both normal clipboard text and an image.
- If your compositor supports PRIMARY selection, select text without pressing
  Ctrl-C and ask the client to read `selection="primary"`.

### Linux X11

- Confirm `xclip` is installed and `$DISPLAY` is set.
- Run `uvx mcp-clipboard --check` and confirm the detected backend is X11.
- Test CLIPBOARD and PRIMARY separately. They should not overwrite each other.
- Include whether you are using a local desktop, SSH X forwarding, VNC, or a
  nested server such as Xephyr.

### macOS

- Confirm `pbcopy` and `pbpaste` are available.
- Test plain text, markdown, URL, and image clipboard flows.
- For rich content, note the source and target apps because UTI behavior varies
  across Safari, Chrome, Notes, Slack, terminal apps, and editors.
- Include whether the MCP client was launched before or after installing the
  package runner, since GUI apps can inherit stale PATH values.

### Windows

- Run from a normal PowerShell or Windows Terminal session, not only WSL.
- Test non-ASCII text such as curly quotes, em dash, CJK, Arabic, and emoji.
- Test plain text, markdown, URL, and image clipboard flows.
- Include the PowerShell version and code page if text comes back corrupted:

```powershell
$PSVersionTable.PSVersion
chcp
```

## Repro Report Template

```markdown
### Environment

- OS:
- Desktop/session:
- MCP client:
- mcp-clipboard version:
- Install runner:
- Clipboard backend:

### What I tested

- [ ] Plain text read/write
- [ ] Long command write
- [ ] Code block read
- [ ] URL read
- [ ] Markdown write
- [ ] Image read/write
- [ ] Format listing
- [ ] PRIMARY selection, if Linux

### Result

What worked:

What failed:

Exact prompt:

Tool output or error:

Debug logs:
```

For debug logs, restart the server with:

```bash
MCP_CLIPBOARD_DEBUG=1 uvx mcp-clipboard
```

Avoid posting private clipboard contents in public issues. Replace sensitive
text with a minimal synthetic example that still reproduces the behavior.
