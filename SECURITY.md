# Security Policy

## Supported versions

mcp-clipboard is currently on the 2.0.x line. Fixes for security
issues are applied to the latest published version only. Users of
earlier versions should upgrade.

| Version | Supported         |
| ------- | ----------------- |
| 2.0.x   | ✅ security fixes |
| < 2.0   | ❌ upgrade        |

## Reporting an issue

**Please do not file a public GitHub issue for security problems.**

The only supported channel is a **GitHub Private Security Advisory**.
To open one:

1. Go to <https://github.com/cmeans/mcp-clipboard/security/advisories/new>.
2. Fill in a description, steps to reproduce, and the affected
   version.
3. Submit as a draft advisory. Only the maintainer will see it.

This creates a private thread where the report, any proof-of-concept,
the fix, and disclosure timing can be discussed without exposing the
issue publicly. The private vulnerability reporting feature is
enabled on this repository.

If you cannot use GitHub Private Security Advisories for some reason,
please open a **public** issue titled simply "Security contact
request" — no details — and the maintainer will reach out to arrange
a private channel.

## Please include

- A description of the issue and its impact.
- Steps to reproduce (or a proof-of-concept).
- The version of mcp-clipboard affected.
- Your operating system and clipboard backend (Wayland, X11, macOS,
  Windows).

## What to expect

- **Acknowledgment** after the maintainer sees the report. Response
  times vary — this is a one-person project.
- **Coordinated fix timeline.** mcp-clipboard is maintained by one
  person, not a security team. Please be patient.
- **Credit in the release notes** if you'd like it. Anonymous
  disclosure is also fine.
- **No monetary reward.** mcp-clipboard does not operate a bug
  bounty program. Reports are voluntary contributions to project
  safety.

## Scope

**In scope**

- Argument smuggling or unsafe subprocess invocation via clipboard
  content (`wl-paste`, `xclip`, `pbpaste`, `osascript`, PowerShell for
  the Windows image read/write paths). Issues in pywin32-based code
  paths (Windows text/plain, text/html, text/rtf, image/svg+xml) are
  also in scope; those bypass subprocess invocation entirely and call
  the Win32 clipboard API directly, so the surface differs.
- Output-injection issues in any of the supported output formats
  (markdown, json, csv, slack, jira, confluence, html, notion).
- HTML output producing markup that could execute in a browser when
  rendered.
- Unsafe handling of binary clipboard data (images, audio, video).
- Supply chain or packaging issues affecting published wheels or
  sdists on PyPI.

**Out of scope**

- Vulnerabilities in dependencies — please report those upstream to
  the affected dependency.
- Attacks that require an adversary to already have write access to
  the system clipboard (that's a compromised system, not a
  clipboard-specific issue).
- Oversized-input handling — clipboard reads are truncated at
  50,000 characters by design.
- Issues in the MCP protocol itself — please report to
  `modelcontextprotocol/specification`.

## Historical issues

Security-relevant findings are tracked in the GitHub issue tracker
under the `security` label. See also the [`LICENSE`](LICENSE) file
for Apache-2.0 warranty disclaimers.
