# Contributing to mcp-clipboard

Thanks for your interest. This is a small utility project with a simple
contribution process. Please read this whole document before opening
your first PR — it's short.

## License of your contribution

**By submitting a pull request, you agree that your contribution is
licensed under the [Apache License 2.0](LICENSE)**, the same license
as the rest of the project.

This is the "inbound = outbound" rule defined in Apache-2.0 § 5:

> Unless You explicitly state otherwise, any Contribution intentionally
> submitted for inclusion in the Work by You to the Licensor shall be
> under the terms and conditions of this License, without any
> additional terms or conditions.

In plain English:

- **You retain copyright on your own code.** You are not transferring
  ownership to the maintainer.
- **You grant everyone a perpetual, irrevocable, royalty-free license**
  to use, modify, redistribute, and sublicense your contribution under
  Apache-2.0.
- **You grant a patent license** covering any patents you hold that
  read on your contribution (Apache-2.0 § 3).
- **You cannot attach additional terms** to a contribution. If your
  PR body, commit messages, or comments propose extra restrictions —
  compensation claims, bounty invoices, attribution beyond what
  Apache-2.0 already requires, "please don't use this commercially,"
  etc. — those have no legal effect under § 5 and the PR will be
  asked to remove them before review.

If you can't agree to those terms, please don't submit a PR.

## No bounties or paid contributions

mcp-clipboard does not offer bug bounties, paid contributions, or any
kind of reward program. All contributions are voluntary donations
under Apache-2.0.

Attaching a wallet address, invoice, bounty claim, or compensation
request to a PR does not create an expectation of payment. PRs with
such attachments will be asked to remove them before review.

## Before you open a PR

For anything bigger than a one-line fix:

1. **Open an issue first** so we can agree on scope and approach.
   Drive-by PRs for non-trivial changes often get closed because
   they don't match what the project needs.
2. **One concern per PR.** Don't bundle "fix X" with "refactor Y"
   and "add Z." Small focused PRs get reviewed and merged faster.
3. **Check that a similar PR isn't already open.**

## Development setup

```bash
uv sync --extra dev    # install runtime + dev dependencies
uv run pytest          # run the full test suite (mocked, no clipboard needed)
uv run pytest tests/test_parser.py           # single test file
uv run pytest -k "test_format_html"          # single test by name
uv run pytest -m integration               # integration tests (real clipboard)
```

**Integration tests** (`tests/test_integration.py`) exercise real clipboard tools (`wl-paste`, `xclip`, `pbpaste`, etc.) and are skipped by default. Run them with `-m integration` if you have a clipboard daemon available. These are especially useful for platform testers (#5, #10).

For a manual cross-platform checklist and report template, see
[`docs/testing.md`](docs/testing.md).

Requires **Python 3.11+**. See the [`README → Development`](README.md#development) section for additional commands (build, debug logging, MCP Inspector).

## PR requirements

Every PR must:

- **Include a test.** If you're fixing a bug, add a regression test
  that fails on `main` and passes on your branch. If you're adding
  a feature, cover the new code paths. Tests live in `tests/` next
  to what they test (`parser.py` → `tests/test_parser.py`).
- **Add a CHANGELOG entry** under `## [Unreleased]` in
  `CHANGELOG.md`, categorized `### Added` (new feature),
  `### Changed` (behavior change), or `### Fixed` (bug fix). One
  line is enough.
- **Link the issue** with `Closes #N` in the PR body so merging
  auto-closes it.
- **Pass CI locally first** — run `uv run pytest` and confirm green
  before pushing. CI runs the same suite across Python 3.11 / 3.12
  / 3.13.
- **Write a clear commit message.** PRs are squash-merged, so your
  PR title becomes the commit subject and your PR body becomes the
  commit body. Write both as if someone reading `git log` a year
  from now should understand what changed and why.

## PR body format

Two required sections:

    ## Summary

    Two or three sentences on what changed and why.

    ## QA

    A checklist the maintainer can walk to verify the change:

    - [ ] Run `uv run pytest tests/test_<module>.py::test_<name>` — passes
    - [ ] Confirm no regression in the affected tool
    - [ ] Confirm the fixed behavior

    Closes #N

## How the review process works

1. **CI runs first.** For first-time contributors, the maintainer
   has to manually approve the workflow run (GitHub policy for fork
   PRs). Your PR will sit with no checks until a maintainer clicks
   "Approve and run." This is not a signal that you're being
   ignored.
2. **Label automation takes over.** After CI passes, the PR
   auto-promotes from `Awaiting CI` → `Ready for QA`. You don't
   need to do anything.
3. **Maintainer reviews.** If there are issues, the PR gets
   `QA Failed` and a review comment. Push your fix; labels reset
   automatically.
4. **Final maintainer review and merge.** Once QA is clean, the
   maintainer does a final review and merges the PR. All PRs are
   squash-merged. Your branch is auto-deleted after merge.

## Code style

- Python 3.11+; full type hints; `from __future__ import annotations`.
- Stdlib-first: no new runtime dependencies without discussion. The
  only current runtime dep is `mcp[cli]`.
- Test file names mirror source: `src/mcp_clipboard/parser.py` →
  `tests/test_parser.py`.

## Reporting bugs or security issues

Three issue templates are available — please use the right one:

- **[Bug report](../../issues/new?template=bug_report.yml)** —
  something isn't working as documented.
- **[Feature request](../../issues/new?template=feature_request.yml)** —
  a new capability or a change to existing behavior.
- **[Platform test report](../../issues/new?template=platform_test_report.yml)** —
  results from testing on macOS, Windows, or X11 (see #5 and #10).

For **security issues**, see [`SECURITY.md`](SECURITY.md) for private
disclosure instructions. Please don't file public issues for security
problems.

## Contact

File an issue or start a discussion on the repo. This is a
one-person project, so **response times vary** — please be patient.
