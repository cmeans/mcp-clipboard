# Changelog

All notable changes to this project will be documented here.

## [Unreleased]

### Added
- New `clipboard_version` MCP tool that returns the running
  mcp-clipboard package version. Diagnostic surface for hosts and
  agents that don't otherwise expose `serverInfo` to the model
  (notably Claude Desktop on Windows). Test harnesses can now
  record `mcp_clipboard_version` from inside an MCP session
  without depending on a shell or filesystem access.

### Fixed
- Fresh installs crash on startup with `ModuleNotFoundError: No module
  named 'mcp.server.fastmcp'`. The `mcp[cli]` dependency had no upper
  bound, so new installs resolved mcp 2.x (released 2026-07-28), which
  renamed `FastMCP` to `MCPServer`. The dependency is now capped at
  `mcp[cli]>=1.2.0,<2`; migrating to the mcp 2.x API is tracked
  separately. Closes #153
- Windows: non-ASCII characters survive `clipboard_paste` and
  `clipboard_read_raw` round-trips. Em dash (U+2014), curly quotes
  (U+2018-U+201D), ellipsis (U+2026), and non-Latin scripts (CJK,
  Arabic, emoji) were being transliterated to the closest ASCII
  equivalent or substituted with `?` on the way out of PowerShell.
  Root cause: `_windows_read` for text/plain, text/html, and
  text/rtf invoked PowerShell without setting
  `[Console]::OutputEncoding`, leaving stdout encoded with the
  parent's active console code page (typically CP1252 on US-English
  Windows). The clipboard bytes are stored correctly in UTF-16; the
  loss happened only on the way back to Python. Fix prepends
  `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;` to
  every PowerShell-backed `_windows_read` branch, mirroring the
  existing `_WINDOWS_UTF8_PREAMBLE` on the input side from #131.
  The Windows e2e suite captured this directly: mc-103 / mc-301 /
  mc-302 confirm the clipboard bytes round-trip byte-perfect; the
  same input under different parent codepages produced corrupted
  vs. clean reads in the same run (mc-002/003 vs mc-026/027/028).

## [2.6.1] - 2026-05-07

### Fixed
- SVG clipboard round-trip on Windows and macOS: copying an SVG via
  `clipboard_copy(mime_type="image/svg+xml")` and then pasting it back
  no longer returns "Clipboard is empty." Three layered gaps closed:
  (1) `_windows_read` gained an `image/svg+xml` branch that calls
  `Clipboard::GetData('image/svg+xml')` (mirroring the custom format
  string the writer registers) and forces UTF-8 stdout encoding so
  the round-tripped XML doesn't get mangled by PowerShell's default
  OEM `OutputEncoding`. (2) `_macos_read` gained an `image/svg+xml`
  branch that reads `public.svg-image` UTI bytes as UTF-8, and
  `_UTI_TO_MIME` learned `public.svg-image -> image/svg+xml` so
  `list_clipboard_formats` surfaces SVG with its MIME type rather
  than the raw UTI. (3) `clipboard_paste`'s auto-dispatch no longer
  routes `image/svg+xml` through the binary `read_clipboard_image`
  path (which raster readers reject as "Unsupported image type");
  SVG-only clipboards are now read as text and returned in an
  ```svg fenced code block. Raster images still take precedence
  over SVG when both are present, so a typical "screenshot + SVG
  source" clipboard still returns the viewable PNG as before. The
  Linux backends (Wayland/X11) were already correct via passthrough
  on `wl-paste --type` / `xclip -target`.

## [2.6.0] - 2026-05-07

### Fixed
- Windows: non-ASCII characters (em dash, en dash, curly quotes,
  non-Latin scripts, etc.) are no longer corrupted on
  `clipboard_copy` / `clipboard_copy_markdown`. Root cause was that
  PowerShell's `[Console]::In.ReadToEnd()` decodes stdin via
  `[Console]::InputEncoding`, which defaults to the OEM/ANSI code page
  on Windows (commonly CP1252), so the UTF-8 bytes Python piped in
  were misread as separate CP1252 characters before `Set-Clipboard`
  ever ran. Each affected PowerShell write script now sets
  `[Console]::InputEncoding = [System.Text.Encoding]::UTF8` before
  reading stdin. Affects `_windows_write` (text/plain) and
  `_windows_write_typed` for text/plain, text/html (CF_HTML byte
  offsets are now valid for the round-tripped UTF-8 payload),
  text/rtf, and image/svg+xml. The base64-on-stdin paths
  (`_windows_write_multi`, `_windows_write_image`) were already safe
  because base64 alphabets are ASCII-only, so neither needed changing.
  Verified on a QEMU Windows guest where the bug originally
  reproduced. Closes #129.

### Added
- CLI flags `--version`, `--help`, `--check` on the `mcp-clipboard`
  command. Running the binary directly previously gave no signal that
  anything had happened (it would silently start a stdio server and
  block on input), forcing new users to wire it into an MCP client
  before they could verify their install worked at all. The new flags
  give a fast feedback loop: `--version` prints the installed version,
  `--help` summarizes usage and points at the README setup section,
  and `--check` runs platform detection (calling the same
  `_detect_backend()` the runtime uses) and reports whether the
  backend is reachable. `--check` exits 1 with a stderr diagnostic
  when no backend is available so it can also serve as a CI smoke
  check. Closes #130.

- New `github-release` job in `publish.yml` auto-creates (or updates) a
  GitHub Release matching the pushed tag, with notes pulled from the
  matching `## [VERSION]` section of `CHANGELOG.md`. Idempotent: if a
  Release for the tag already exists (hand-crafted, or a re-run of a
  partially-failed publish), the job edits the notes in place via
  `gh release edit` rather than failing with HTTP 422. Falls through to
  `--generate-notes` (auto commit-list) when the CHANGELOG has no
  matching entry. `needs: publish-pypi` so the Release only lands after
  the wheel is on PyPI; `permissions: contents: write` enables
  `gh release create`. Closes the GitHub-Releases-page-drift gap that
  surfaced after v2.4.0/v2.5.0/v2.5.1 all landed on PyPI without
  matching Releases (manually backfilled on 2026-05-06). Pattern ported
  from mcp-synology with a CHANGELOG-format adaptation: mcp-clipboard
  uses Keep-a-Changelog `## [VERSION]` brackets, so the awk anchor is
  `^## \[VERSION\]` rather than mcp-synology's `^## VERSION( |\()`.
  (#127) - closes #126.

### Changed
- Platform status corrected in two README locations and `CLAUDE.md`.
  The previous claim that "Windows implementations are complete but
  untested on real hardware" was stale: in v2.5.x the Windows code
  paths were exercised end-to-end on a QEMU Windows guest, which
  surfaced and resolved the UTF-8 stdin encoding bug (#129).
  Updated copies: the `> **Platform status:**` callout in the Setup
  section, the corresponding bullet in `## Limitations`, and the
  Conventions section of `CLAUDE.md`. X11 and macOS still hold the
  "complete with unit tests but unverified on live hardware" status
  honestly; bug reports and PRs are still welcome there.

- README setup section restructured into a five-step quick-start
  (install a Python package runner → install Linux backend tool →
  verify via `--check` → register with the MCP client → confirm
  end-to-end). Step 1 acknowledges that mcp-clipboard is a regular
  PyPI package and either of the two runners advertised in the
  README's install-counts badges (`pipx` and `uv`) works to install
  and launch it; the section links to the official pipx and uv
  install docs rather than reproducing per-platform commands, so
  the install path stays accurate as upstream packaging changes.
  Steps 3, 4, and 5 show both `pipx run mcp-clipboard` and
  `uvx mcp-clipboard` forms in the verification command, the Claude
  Code/Desktop snippets, and the troubleshooting fallback. Adds a
  Windows-specific note that Claude Desktop caches the environment
  (including `PATH`) at launch, so a runner installed after Claude
  Desktop started won't be visible until the tray-quit-and-relaunch,
  and a pointer to the Claude Desktop MCP log location for
  troubleshooting. The previous structure assumed `uv` was the only
  way to run mcp-clipboard and that running the binary would give
  visible feedback; the new structure reflects the project's actual
  bi-runner support and gives a new user a path from "I just heard
  about this" to "the MCP host is using mcp-clipboard" without
  pinning their tool choice. Companion to the new CLI flags above.
  Closes #130.

- README: surface the X11/Wayland PRIMARY-selection feature as a featured
  `### Bonus` subsection in `## Why This Exists`, alongside the table-paste
  pitch and the fixes-copying-from-Claude-Code pitch. Adds a short
  explanation of CLIPBOARD vs PRIMARY plus four concrete workflow callouts
  (terminal triage, vim/IDE visual selection, browser/PDF reading,
  two-buffer workflows) so a Linux user skimming the README can find the
  feature without reading every parameter description in the tools table.
  Docs only; no code change. Closes #123.

## [2.5.1] - 2026-05-05

### Fixed
- Add `mcp-name: io.github.cmeans/mcp-clipboard` HTML comment to
  `README.md` for MCP-registry ownership validation. The v2.5.0
  `publish-registry` job (the first-ever firing of the registry-
  publish workflow added in #120) failed with
  `registry validation failed for package 0 (mcp-clipboard): PyPI
  package 'mcp-clipboard' ownership validation failed. The server
  name 'io.github.cmeans/mcp-clipboard' must appear as
  'mcp-name: io.github.cmeans/mcp-clipboard' in the package README`.
  The token has to ship in the README that lands on PyPI (not just in
  the repo), so this is a release-time fix: the comment lives at the
  top of `README.md` (matching the mcp-synology convention) and gets
  baked into the wheel by hatchling on build. v2.5.0 stays on PyPI
  but is unregistered; v2.5.1 carries the validating README and will
  register on tag-push. (#125) - closes the v2.5.0 publish-registry
  failure.

## [2.5.0] - 2026-05-05

### Added
- X11 PRIMARY / Wayland primary-selection support on the read tools:
  `clipboard_paste`, `clipboard_read_raw`, and `clipboard_list_formats`
  now accept an optional `selection` argument (`"clipboard"` default,
  `"primary"` for the middle-click / select-text-to-paste buffer).
  Wayland uses `wl-paste --primary`; X11 uses `xclip -selection primary`.
  macOS and Windows have no PRIMARY analog and return a clear
  `selection={selection!r}` error if anything other than `"clipboard"`
  is passed. Power-user workflow: select text in a terminal or browser
  without Ctrl-C, then `clipboard_paste(selection="primary")` returns
  it. Public APIs `read_clipboard`, `list_clipboard_formats`, and
  `read_clipboard_image` gained the same `selection` parameter.
  Linux validated under Xvfb in CI (PRIMARY round-trip plus list
  formats); macOS and Windows reject paths unit-tested. Write-side
  PRIMARY support deferred — issue #110 noted lower priority for it.
  (#122) - closes #110.
- New `clipboard_copy_markdown(text)` tool: render a markdown source string
  to HTML (via `markdown-it-py` with raw-HTML pass-through disabled, so the
  rendered output is safe by construction) and write both `text/html` and
  `text/plain` (the markdown source) to the clipboard. Closes the read/write
  asymmetry for formatted text — today we read HTML tables and rich content
  beautifully but only wrote plain. macOS and Windows write both formats
  atomically via NSPasteboard / `DataObject` so paste targets pick the
  format they prefer (Slack/Gmail/Notion/Discord get the rendered HTML;
  vim/terminal/text editors get the markdown source). Wayland and X11 are
  single-MIME-per-call and write only `text/html`. On Wayland, `wl-copy`
  auto-advertises `text/plain` for UTF-8 content but the bytes returned
  for that target are the rendered HTML markup, not the markdown source
  (a vim user pasting after the tool runs sees `<h1>...` etc); on X11
  the `text/plain` target is genuinely absent. For a plain-text paste of
  the markdown source on either platform, call `clipboard_copy` with the
  markdown source directly. Adds
  `markdown-it-py>=3.0` as a new runtime dependency (pure Python, ~250 KB,
  no native deps). New private dispatcher `write_clipboard_multi_format`
  in `clipboard.py` backs the four per-backend writers
  (`_wayland_write_multi`, `_x11_write_multi`, `_macos_write_multi`,
  `_windows_write_multi`); single-MIME backends pick `text/html` over
  `text/plain` over the first dict entry. Linux validated under Xvfb in
  CI; macOS and Windows unit-tested only per repo convention. RTF generation
  intentionally deferred (cross-platform RTF is fiddly enough to deserve
  its own ticket). (#121) - closes #109.
- Register with the MCP Server registry at
  `registry.modelcontextprotocol.io`. New `server.json` (root) carries
  the registry manifest; `scripts/sync-server-json.py` is the
  single-source-of-truth sync from `pyproject.toml`'s `[project].version`
  to `server.json`'s two version fields, with a `--check` mode that CI
  uses to fail PRs that drift. New composite action
  `.github/actions/install-mcp-publisher` pins `mcp-publisher` to the
  v1.7.6+ audience binding (the registry rolled out a new OIDC audience
  on 2026-04-30 that older `mcp-publisher` releases fail authentication
  against). `ci.yml` gains `version-sync` and `validate-server-json`
  jobs; `publish.yml` gains a release-time `validate-server-json` gate
  (now a `needs:` of the renamed `publish-pypi` job) and a new
  `publish-registry` job that runs after `publish-pypi` (the registry
  validates the referenced PyPI package+version before accepting the
  entry). The registry publish is idempotent — duplicate-version errors
  from rerunning a partially-failed tag are treated as a no-op. (#120) -
  closes #114.

## [2.4.0] - 2026-05-05

### Fixed
- macOS clipboard write paths (`_macos_write_typed` for HTML/RTF/SVG and
  `_macos_write_image` for PNG/JPEG) now pipe their AppleScript over
  stdin via `osascript -` instead of passing it as a single `-e <script>`
  argv element. The previous form would have hit macOS's default
  ~1 MiB `ARG_MAX` cap for any payload above ~750 KB after base64
  framing — typical phone photos (1-5 MB JPEG) tripped this in practice
  via the new `clipboard_copy_image` path. Chunked-base64 source
  formatting is preserved (AppleScript's per-line parser limit applies
  even to scripts read from stdin), and a shared `_macos_pasteboard_script`
  helper now backs both write paths. Two new regression tests assert the
  argv length stays under `ARG_MAX` regardless of payload size, mirroring
  the post-#111 Windows pattern. (#117) - closes #113.

### Added
- `clipboard_copy` now accepts `mime_type="image/svg+xml"`. SVG is XML
  and the read path already treated it as text-readable, but the write
  path was rejecting it as binary. Apps that consume SVG from the
  clipboard (Edge, Chrome, Figma, Inkscape) now receive the rendered
  image. Wayland/X11 pass-through with zero new code; macOS uses the
  `public.svg-image` UTI on NSPasteboard; Windows uses a `DataObject`
  custom format `image/svg+xml`. Linux validated under Xvfb in CI.
  Note: this is single-MIME write — only `image/svg+xml` is set on the
  clipboard. On Wayland, `wl-copy` happens to also advertise `text/plain`
  automatically, so non-SVG-aware editors get the source for free; on
  X11 / macOS / Windows there is no text fallback until multi-format
  simultaneous write lands (#109). (#116) - closes #112.
- New `clipboard_copy_image(image_data, mime_type)` tool: write a PNG or
  JPEG to the system clipboard from base64-encoded bytes. Closes the
  read/write asymmetry for binary content (we already returned six image
  formats on read but had no way to put one back). Pass-through with no
  re-encoding — no new runtime dependencies. Magic bytes are validated
  against the declared MIME type, and the existing
  `MCP_CLIPBOARD_MAX_IMAGE_BYTES` cap (default 10 MB) is enforced. All
  four backends supported: Wayland (`wl-copy --type`), X11
  (`xclip -selection clipboard -target -i`), macOS (NSPasteboard
  `setData:forType:` via osascript), Windows
  (`Clipboard::SetImage` via PowerShell, base64 piped over stdin so the
  payload never touches `CreateProcess`'s 32,767-char `lpCommandLine`
  cap). Linux validated under Xvfb in CI; macOS and Windows unit-tested
  only per repo convention.
  Other formats (GIF, WebP, TIFF, BMP) intentionally deferred — can land
  in a follow-up without taking on Pillow. (#111) - closes #108.

### Changed
- Sweep British to American spellings in source comments, test fixtures,
  and historical CHANGELOG prose: `behaviour` → `behavior` in
  `_run_subprocess` docstring; `cancelled` → `canceled` in narrative
  comments, test assertion messages, and one parser test fixture string.
  `asyncio.CancelledError` (Python class name) is left as-is. (#107) -
  closes #106.

## [2.3.0] - 2026-05-02

### Added
- Public `clipboard.reset_backend_cache()` helper. Replaces the previous
  pattern of poking the module-private `_backend` global directly (which
  the autouse fixtures in `tests/test_server.py` and
  `tests/test_integration_x11.py` were already doing). Production code
  has no reason to call this; it exists for tests that switch backends
  or re-read `MCP_CLIPBOARD_BACKEND` mid-process. (#104)
- Direct unit tests for `parser._has_header_row` covering the
  `len(rows) < 2` early-return path and the all-text-no-header path,
  plus a positive case for text-headers-over-numeric-data. Previously
  exercised only indirectly via `format_table` JSON output. (#104)
- Coverage for `clipboard_paste` `include_schema=True` when the data
  rows are wider than the header row -- the padding loop at
  `server.py:222-228` now has a regression test that asserts synthetic
  `Col 3` / `Col 4` labels are emitted. (#104)
- Coverage for `MCP_CLIPBOARD_MAX_WRITE_BYTES` and
  `MCP_CLIPBOARD_MAX_IMAGE_BYTES` env-var validation. Both vars are
  parsed via `int(os.environ.get(...))` at module import; a non-integer
  value raises `ValueError` before anything else runs. Tests exercise
  each in a subprocess so the partial-import state stays out of the
  in-process module cache. (#104)
- Headless X11 integration tests (`tests/test_integration_x11.py`) and a
  matching CI `integration-x11` job that runs them against a real `xclip`
  process under Xvfb. Five round-trip tests exercise plain text, unicode,
  HTML via `-target`, format listing after a typed write, and binary
  image read. Closes the gap from the audit that the X11 stack
  (`_x11_read`, `_x11_list_formats`, `_x11_read_image`, `_x11_write`,
  `_x11_write_typed`) was mock-only despite shipping in production
  builds. (#102)
- README badge breakouts for installer mix (pip, pipenv, pipx, uv,
  poetry, pdm) and OS distribution (linux, macos, windows), powered by
  the per-installer (v0.2.0) and per-OS (v0.3.0) endpoints from
  pypi-winnow-downloads. Existing hero `Downloads` badge is unchanged.
- `.github/PULL_REQUEST_TEMPLATE.md` auto-fills new human-authored
  PR bodies with Summary, Test plan (matching the CI's `pytest`,
  `ruff check`, `ruff format --check`, `mypy` invocations), and
  CHANGELOG-confirmation sections. Dependabot bypasses the template
  and is handled by the auto-CHANGELOG workflow instead.
- `.github/workflows/dependabot-changelog.yml` auto-prepends a
  `## [Unreleased]` → `### Changed` entry to Dependabot-authored PRs
  so they satisfy the per-PR CHANGELOG rule without manual
  intervention. Runs on `pull_request_target`, filters to
  `dependabot[bot]`, mints a GitHub App installation token via
  `actions/create-github-app-token`, fetches metadata via
  `dependabot/fetch-metadata@v3.1.0` (the v3 line fixed empty
  `prevVersion`/`newVersion` on grouped PRs), and pushes the
  CHANGELOG commit under the `cmeans-claude-dev[bot]` identity.
  Subsection insertion respects Keep-a-Changelog v1.1.0 ordering
  (Added → Changed → Deprecated → Removed → Fixed → Security) so a
  newly-created `### Changed` block lands in the right position.
  Loop guard skips when the last commit is already by the bot;
  idempotency guard skips when the PR number is already referenced
  in `CHANGELOG.md`. Operator must configure two repo secrets
  (`BOT_APP_ID`, `BOT_APP_PRIVATE_KEY`) before the workflow can run.
- `CLAUDE.md § Conventions` documents the per-PR CHANGELOG rule and
  the Keep-a-Changelog category set, mirroring the conventions
  already in place across `cmeans/mcp-synology` and
  `cmeans/pypi-winnow-downloads`.
- Dependabot version-update configuration (`.github/dependabot.yml`)
  for pip and github-actions ecosystems. Weekly schedule (Monday
  06:00 America/Chicago), grouped per ecosystem to reduce noise.
  Commit-message uses `prefix: chore` with `include: scope` so
  Dependabot's auto-appended `(deps)` scope yields canonical
  `chore(deps): bump <foo>` subjects (avoids the doubled-prefix
  failure mode documented in the dependabot-pr-hygiene playbook).
  Labels `dependencies`, `python`, and `github-actions` are
  pre-created on the repo so PRs are categorized on creation.

### Changed
- `clipboard_read_raw` instruction text no longer enumerates a fixed list
  of `application/*` types as if they were specially permitted. The
  implementation only blocks the `image/`, `audio/`, and `video/`
  prefixes plus the exact type `application/octet-stream`, so any other
  MIME type (text/*, application/json, application/xml,
  application/xhtml+xml, image/svg+xml, etc.) passes through. The
  reworded text reflects that, removing the maintenance trap where the
  enumeration would silently drift from the code if the blocklist
  changes. (#102)
- Defensive comment on the `_format_non_tabular` JSONDecodeError fallback
  in `server.py:189-194` documenting that the branch is currently
  unreachable: `detect_content_type` only returns `"json"` after a
  successful `json.loads` on the same (already-truncated) text the
  caller will re-parse. Kept as a safety net in case content-type
  detection ever drifts. No behavioral change. (#102)
- README downloads badge now points at the dogfooded
  `cmeans/pypi-winnow-downloads` endpoint
  (`pypi-badges.intfar.com/mcp-clipboard/downloads-30d-non-ci.json`)
  instead of `shields.io/pypi/dm/mcp-clipboard`. The new number is
  30-day non-CI installs (the metric that signals real adoption);
  the previous shields.io built-in counted CI/installer/mirror
  traffic. Badge link target swapped from the PyPI project page
  (already covered by the `PyPI version` badge above) to
  `github.com/cmeans/pypi-winnow-downloads` so a click surfaces the
  source-of-truth methodology. Closes #97.
- **Bump github-actions group: codecov/codecov-action 5→6** (#95)
- Add `workflow_dispatch:` trigger to `pr-labels-ci.yml` to restore
  template parity with `cmeans/yt-dont-recommend`. Gives maintainers a
  manual "Run workflow" handle and primes the `workflow_run` dispatcher
  when the file is first introduced to a fresh repo. No-op on existing
  PRs because the job-level `if:` guards require `workflow_run` context.
  Closes #89.

### Removed
- Glama integration. The README Glama score badge and the `glama.json`
  registry metadata file are gone. Supersedes the in-Unreleased badge
  swap from PR #100 (which is now moot). (#103)

### Fixed
- Harden GitHub Actions context handling against shell script injection in
  `pr-labels-ci.yml` and `qa-gate.yml`. Contributor-controlled values
  (notably `workflow_run.head_branch` from fork PRs) and label/sha/repo
  context are now passed through `env:` blocks instead of being interpolated
  directly into `run:` shell. Aligns with the pattern already used in
  `pr-labels.yml`. Closes #87.
- Remove the literal empty GitHub Actions expression from two shell
  comments in `pr-labels-ci.yml`. GHA substitutes such sequences inside
  `run:` blocks before the shell sees them (including within comments),
  and the queue-time parser rejects the empty form on `workflow_dispatch`.
  The `workflow_run` path tolerated it, so the bug was latent here but
  blocked manual dispatch and fresh-repo cascades. Closes #91.
- Windows clipboard `list_formats` now deduplicates MIME types when multiple
  native format names map to the same MIME (e.g. `Text` and `UnicodeText`
  both map to `text/plain`), matching the existing macOS behavior. Prevents
  inflated format counts and downstream duplicate iteration. (#101)
- Nested HTML tables no longer leak inner-table cell text into the outer
  cell. `_TableExtractor.handle_data` now gates on `_table_depth == 1` so
  text inside an inner `<table><tr><td>...</td></tr></table>` is no longer
  concatenated into the surrounding outer cell. (#101)
- macOS `_macos_write_typed` no longer trips AppleScript's 32,767-character
  per-line limit when writing HTML or RTF content larger than ~24 KB. The
  base64-encoded payload is split across multiple `set b64 to b64 & "..."`
  statements with a 4,000-character chunk size. (#101)
- `_run_subprocess` and `_run_with_stdin` no longer orphan their child
  process when the calling task is canceled (e.g. on MCP client
  disconnect). A `finally` block now calls `proc.kill()` on any non-normal
  exit including `asyncio.CancelledError`, which inherits from
  `BaseException` and previously bypassed the timeout-only `except`
  handler. (#101)
- `parse_tsv` no longer treats single-cell input with a stray tab
  (`"word\t"`, commonly produced when copying one Excel cell on Windows)
  as a 1x2 table with a phantom empty column. Single-row results now
  require at least two non-empty cells. (#101)

### Security
- New `MCP_CLIPBOARD_MAX_IMAGE_BYTES` cap (default 10 MB) on
  `read_clipboard_image`. A 100 MB clipboard bitmap previously became
  ~133 MB base64 in a single MCP response and could time out or drop
  the MCP transport. Oversized reads now raise the new
  `ClipboardSizeError`, and `clipboard_paste` returns an explanatory
  message instead of forwarding the payload. The cap is a wire-level
  guard rather than a memory-bounded read: the backend still buffers
  the full image before the size check, so the inflated wire response
  is prevented but local memory pressure on the host running the
  server is not. (#101)
- Image subtype passed to `mcp.Image(format=...)` is now validated against
  an allowlist (`png`, `jpeg`, `gif`, `webp`, `tiff`, `bmp`). Clipboard-
  controlled MIME strings with parameter injection or unexpected subtypes
  fall back to `png` rather than flowing through to the host. (#101)
- Markdown code fences in `clipboard_paste` (JSON, code, RTF branches) now
  size dynamically to one longer than the longest backtick run inside the
  wrapped content. Prevents clipboard text containing literal triple
  backticks from closing the fence early and rendering injected content
  as Markdown (or HTML on permissive hosts). (#101)

## [2.2.1] - 2026-04-16

### Added
- Strengthened `test_paste_large_content_truncated` with a size-bound
  assertion so it can no longer pass if truncation regresses. Closes #70.
- Strengthened `test_format_destination_ragged_rows` with per-format
  structural assertions. Previously only checked non-empty output;
  now verifies cell-count uniformity for jira/confluence (`||`/`|` cell
  parsing) and html (`<th>`/`<td>` counts), so a formatter that dropped
  or failed to pad the short row would be caught. Closes #71.

### Changed
- `tests/test_server.py` adds an autouse fixture that resets the module
  global `cb._backend` around every test, replacing scattered manual
  `cb._backend = None` lines in backend-detection and env-override tests.
  Removes the ordering-dependent state hazard called out in #72. Closes #72.

### Fixed
- Subprocess reaping on timeout in `_run_subprocess` and `_run_with_stdin`.
  After `proc.kill()`, the process is now awaited via
  `await asyncio.wait_for(proc.wait(), timeout=1.0)` so the pipes are
  drained and the child is fully reaped, rather than left pending for the
  asyncio child watcher. Closes #69.
- `detect_content_type` no longer misclassifies English prose containing
  "from " or "import " (and other strong code keywords) as code. Strong
  patterns now anchor to the start of a line (after leading whitespace),
  so phrases like "data from the system" or "each import before release"
  are correctly treated as text, while real `import os` / `from x import y`
  at a line start are still detected as code. Closes #68.
- `clipboard_paste` instruction file now describes the `slack` format
  accurately: monospace code block with a dashed-underline header row.
  The prior description ("`*bold*` header + space-aligned data") reflected
  the pre-#50 implementation and was stale. Closes #73.
- `detect_content_type` no longer misclassifies prose that starts with a
  lowercase strong-pattern keyword. Strong patterns were converted from
  substring checks to MULTILINE regex patterns that require syntactic
  context: `def X(`, `let/var/const X =`, `import X$`, `from X import Y`,
  etc. Prose like "let me know", "from the desk of", "import tariffs
  affect trade", "def leaves me confused", and "var is short for variable"
  is now correctly classified as text, while real code forms are still
  detected. Closes #77.
- macOS `_macos_list_formats` now deduplicates MIME types. Both
  `public.utf8-plain-text` and `public.plain-text` map to `text/plain`;
  when both UTIs are on the pasteboard, the result list previously
  contained `text/plain` twice. Closes #74.
- `clipboard_copy` now validates `mime_type` against `_MIME_RE` before
  writing to the clipboard. Previously only `clipboard_read_raw` validated;
  invalid values like `not-a-mime` or `123/456` passed through unchecked
  to the backend subprocess. Closes #75.

## [2.2.0] - 2026-04-12

### Added
- `clipboard_copy` now enforces a write-side size limit (default 1 MiB).
  Rejects oversized content with a clear error message. Override via
  `MCP_CLIPBOARD_MAX_WRITE_BYTES` env var. Closes #27.
- `MCP_CLIPBOARD_BACKEND` env var to override auto-detected clipboard
  backend. Valid values: `wayland`, `x11`, `macos`, `windows`. Useful
  for debugging wrong-backend issues. Closes #29.
- `_run_with_stdin` now captures stderr when `MCP_CLIPBOARD_DEBUG=1`
  and includes it in the `ClipboardError` message on write failures.
  Stderr is still sent to /dev/null in normal mode to avoid the
  wl-copy pipe-deadlock issue. Closes #32.
- Opt-in integration test suite (`tests/test_integration.py`) that
  exercises real clipboard tools. Skipped by default; run with
  `uv run pytest -m integration`. Covers text round-trip, unicode,
  multiline, special characters, format listing, and unavailable
  MIME types. Closes #23.
- `py.typed` PEP 561 marker so downstream type checkers (mypy, pyright)
  see the package's type annotations. Closes #33.

### Fixed
- `_TextExtractor` now uses a depth counter instead of a boolean for
  skipping `<script>`/`<style>` content. Prevents `</style>` from
  prematurely re-enabling text extraction when inside a `<script>` tag
  (and vice versa). Closes #36.
- MIME type validation regex now requires type and subtype to start
  with a letter. Rejects nonsensical values like `123/456` and `_/_`.
  Also validates parameter syntax (`name=value`). Closes #35.
- JSON output now uses type inference to detect headers. When the first
  row's types differ from the data (e.g., text header over integer data),
  it's used as keys. When all rows have matching types, all data is
  preserved as a flat array or list of lists. Removes the undocumented
  `{"values": row}` wrapper. Closes #30.
- `_format_html` now escapes cell values with `html.escape()`, preventing
  XSS via injected `<script>` tags or other HTML in cell content.
  Closes #15.

## [2.1.1] - 2026-04-12

### Fixed
- `detect_content_type` no longer false-positives on prose containing
  common English words like "return", "class", or "public", or operators
  like `->`, `||`, `::`. Short/ambiguous patterns now require 2+ distinct
  matches to classify as code. Closes #20.
- `_windows_read_image` now honors the requested MIME type instead of
  always returning PNG. Maps `image/jpeg`, `image/bmp`, `image/gif`,
  and `image/tiff` to their .NET `ImageFormat` equivalents. Rejects
  unsupported types with `ClipboardError`. Closes #34.
- Truncation message now says "50,000 characters" instead of "50KB".
  The limit is a character count, not a byte count. Constant renamed
  from `_MAX_CONTENT_LEN` to `_MAX_CONTENT_CHARS`. Closes #37.
- `__version__` no longer crashes with `PackageNotFoundError` when
  running from source without installing. Falls back to `"0.0.0+dev"`.
  Closes #28.
- Date inference in `_classify_cell` now short-circuits on values with
  no digits, skipping up to 10 exception-driven `strptime` calls per
  text cell. Closes #26.

## [2.1.0] - 2026-04-12

### Added
- QA workflow labels and `pr-labels` / `qa-gate` automation, matching
  `mcp-synology` and `mcp-awareness`. Adds `Dev Active`, `Awaiting CI`,
  `Ready for QA`, `QA Active`, `Ready for QA Signoff`, `QA Failed`,
  `QA Approved`, `CI Failed`, `merge-order: 0`–`3`, and `dependencies`
  labels. New workflows: `pr-labels.yml`, `pr-labels-ci.yml`,
  `qa-gate.yml`.
- `CONTRIBUTING.md` — license of contribution (Apache-2.0 § 5
  inbound=outbound), no-bounty policy, dev-env setup, PR
  requirements, review process walkthrough, issue template guide,
  and code style notes.
- `SECURITY.md` — private disclosure via GitHub Private Security
  Advisories (the only supported channel; no email fallback), scope,
  and response expectations. Private vulnerability reporting enabled
  on the repository.
- `CODE_OF_CONDUCT.md` — adopts Contributor Covenant 2.1 by
  reference; reports route through a Private Security Advisory
  titled `Conduct` as a workaround for GitHub's lack of a general
  private-contact channel. Closes #25.
- Parametrized escaping test matrix covering special characters
  (pipes, backslashes, angle brackets, quotes, backticks, newlines,
  multibyte) across all 8 output formats. Closes #18.
- `ruff` linter/formatter and `mypy` type checker added to CI as
  separate jobs. Configured in `pyproject.toml`. Closes #22.

### Fixed
- Pipe (`|`) and backslash (`\`) in table cell values are now escaped in the
  `markdown` and `notion` output formats, preventing column-structure corruption
  in rendered tables. Closes #16.
- Pipe (`|`) and backslash (`\`) in table cell values are now escaped in the
  `jira` and `confluence` output formats, preventing cell-boundary corruption
  and accidental header syntax. Closes #17.
- `_macos_read_image` now rejects MIME types without a known UTI mapping
  instead of interpolating raw caller input into an AppleScript string
  literal. Prevents potential script injection via crafted MIME types.
  Closes #24.
- `parse_tsv` now uses `csv.reader` with RFC 4180 quoting instead of
  naive `str.split("\t")`. Fields containing embedded tabs or newlines
  are preserved when wrapped in double quotes. Closes #21.
- Slack table format now renders the entire table (header + data) inside
  a single code block with a dashed underline separator. This avoids
  special character corruption (`*`, `` ` ``) from Slack's mrkdwn
  formatting and fixes header/data column misalignment from mixed
  proportional/monospace fonts. Closes #19, closes #31.

### Changed
- Renamed `.github/workflows/test.yml` → `ci.yml` and the workflow
  `name:` from `Tests` to `CI` for cross-repo consistency. README
  badge URL updated to match.

## [2.0.2] - 2026-04-05

### Added
- Test coverage reporting via pytest-cov and Codecov (96% coverage)
- Coverage badge in README
- PyPI, CI, license, and download badges in README

### Fixed
- README images now use absolute GitHub URLs so logos render on PyPI

## [2.0.1] - 2026-04-04

### Added
- Server icons (light/dark SVG) embedded as data URIs via FastMCP `icons` parameter

### Fixed
- Claude Code install command now uses `--scope user` for global availability

## [2.0.0] - 2026-04-04

### Changed
- **Breaking**: Renamed PyPI package from `clipboard-mcp-server` to `mcp-clipboard`
- **Breaking**: Renamed Python package from `clipboard_mcp` to `mcp_clipboard`
- **Breaking**: CLI command renamed from `clipboard-mcp` to `mcp-clipboard`
- **Breaking**: Debug env var renamed from `CLIPBOARD_MCP_DEBUG` to `MCP_CLIPBOARD_DEBUG`
- License changed from MIT to Apache 2.0
- README rewritten with Claude Code clipboard padding workaround as key feature

## [1.4.0] - 2026-03-15

### Added
- `clipboard_copy` gains a `mime_type` parameter (default: `text/plain`) for writing typed clipboard content
- Wayland and X11: any `text/*` MIME type is passed through to `wl-copy --type` / `xclip -target`
- macOS: `text/html` written via NSPasteboard (`public.html` UTI); `text/rtf` via `public.rtf` UTI; both use base64 encoding to safely pass content through `osascript`
- Windows: `text/html` written with the CF_HTML byte-offset header format; `text/rtf` written via `DataFormats::Rtf`
- Binary MIME types (`image/*`, `audio/*`, `video/*`, `application/octet-stream`) are rejected with a clear error message
- New `write_clipboard_typed(content, mime_type)` in `clipboard.py`; `_windows_html_clipboard_wrap()` helper for CF_HTML formatting

### Limitations
- Writing multiple MIME types atomically (e.g. both `text/html` and `text/plain`) in a single clipboard operation is not supported on Wayland/X11 — doing so requires owning the clipboard selection across calls

## [1.3.0] - 2026-03-15

### Added
- Five new `output_format` values for destination-aware table formatting:
  - `slack` — `*bold*` header line followed by space-aligned data in a monospace code block
  - `jira` — `||Header||` / `|Cell|` Jira wiki markup
  - `confluence` — same as `jira` (shared Atlassian wiki syntax)
  - `html` — `<table>` with `<thead>`/`<th>`/`<tbody>`/`<td>`
  - `notion` — standard GFM pipe table (Notion renders these natively; same output as `markdown`)
- README: new "Destination-aware output formats" section with format/destination table and example phrases

## [1.2.0] - 2026-03-15

### Added
- Table schema inference: new `infer_column_types(rows)` in `parser.py` infers a data type per column (integer, float, currency, percentage, date, boolean, text); majority-wins per column; empty cells skipped; header excluded
- `clipboard_paste` gains `include_schema: bool = False` parameter — when `True` and the clipboard contains a table, a **Column types** table is appended after the data
- Date detection supports ISO 8601 (`datetime.fromisoformat`) plus common regional formats (`MM/DD/YYYY`, `DD/MM/YYYY`, `Month DD, YYYY`, etc.)
- Currency detection handles `$`, `£`, `€`, `¥` prefix and suffix forms with optional thousands separators

## [1.1.0] - 2026-03-15

### Added
- RTF clipboard read support (`text/rtf`) on macOS (via `osascript`/NSPasteboard) and Windows (via PowerShell/`DataFormats::Rtf`)
- `clipboard_paste` Strategy 3: when HTML and plain text are both empty, attempts `text/rtf` as a fallback before checking for binary formats; RTF content is returned in a fenced code block labeled "rich text (RTF)", truncated at 50KB
- Wayland and X11 backends already supported `text/rtf` via pass-through MIME to `wl-paste`/`xclip`

## [1.0.1] - 2026-03-14

### Fixed
- `clipboard_paste` was missing a `-> str | Image` return type annotation, which
  could affect MCP schema generation
- `_BINARY_MIME_PREFIXES` contained `"application/octet-stream"` as a pseudo-prefix
  rather than an exact match; split into a separate `_BINARY_MIME_EXACT` set
- `_TEXT_READABLE_MIMES` contained `application/json`, `application/xml`, and
  `application/xhtml+xml` which were never reachable by the prefix guard; only
  `image/svg+xml` is now listed
- Exit code 1 was silently swallowed for macOS (`osascript`, `pbpaste`) and Windows
  (PowerShell) backends, hiding real errors; exit-1-as-empty is now restricted to
  Wayland and X11 backends

### Changed
- `_base_mime_type()` renamed to `base_mime_type()` (public API)
- `_run()` and `_run_binary()` refactored to share a common `_run_subprocess()`
  core, eliminating ~20 lines of duplicated error handling
- `_configure_logging()` moved from module import time into `main()` so that
  importing `clipboard_mcp.server` no longer configures root logging as a side
  effect
- Added basic MIME type format validation in `clipboard_read_raw` to reject
  malformed input before passing it to a subprocess
- Removed `"use "` from code-detection patterns in `parser.py` to avoid false
  positives on ordinary English text

### Known limitations
- `clipboard_paste` has no return type annotation (`-> str | Image`) because
  FastMCP passes the annotation through Pydantic's `create_model()`, which cannot
  generate a schema for `Image`. The omission is intentional; see the comment in
  `server.py` for details.

### Metadata
- PyPI classifier updated from "Development Status :: 3 - Alpha" to
  "Development Status :: 5 - Production/Stable"
- Added PyPI keywords for better discoverability
- Publish workflows now run the test suite before building and publishing
- README clarifies that X11 has unit test coverage but has not been verified on a
  live X11 session

## [1.0.0] - 2026-03-14

### Added
- Features, Acknowledgments, and copyright sections in README
- GitHub Actions updated to latest major versions

## [0.1.3] - 2026-03-14

### Added
- `clipboard_copy` tool — write text to the system clipboard via `wl-copy`/`xclip`/
  `pbcopy`/PowerShell
- Image passthrough — `clipboard_paste` now returns clipboard images as base64-encoded
  MCP image content blocks that Claude can see and analyze
- `clipboard_read_raw` now accepts `image/svg+xml`, `application/json`,
  `application/xml`, and `application/xhtml+xml` (previously rejected as binary)

### Fixed
- MIME parameter suffixes (e.g. `text/plain;charset=utf-8`) are now stripped before
  comparison, fixing format fallback on some Wayland compositors
- `wl-copy` timeout: stdout/stderr are now redirected to `/dev/null` to avoid
  blocking on the background child process that holds the clipboard

## [0.1.2] - 2026-03-14

### Added
- X11 (`xclip`) backend unit tests
- GitHub Actions CI workflow (`test.yml`) running tests on Python 3.11–3.13

### Fixed
- Improved error handling and test coverage gaps identified in code review

## [0.1.1] - 2026-02-15

### Fixed
- `uvx` and `pipx` install instructions corrected to use `--from clipboard-mcp-server`
  with the `clipboard-mcp` command name

## [0.1.0] - 2026-02-15

### Added
- Initial PyPI release as `clipboard-mcp-server`
- GitHub Actions trusted publisher workflows for PyPI and TestPyPI
- `clipboard_paste` — read tables (HTML/TSV → Markdown/JSON/CSV), JSON, URLs, code,
  and plain text from the clipboard
- `clipboard_read_raw` — return raw clipboard content for a given MIME type
- `clipboard_list_formats` — list available MIME types on the clipboard
- Wayland auto-detection via `$XDG_RUNTIME_DIR` socket scan
- Platform support: Wayland, X11, macOS (osascript/pbpaste), Windows (PowerShell)
