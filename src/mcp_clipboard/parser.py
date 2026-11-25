"""Parse clipboard content: HTML tables, plain text, and content-type detection.

Uses only the Python standard library (html.parser). Designed to handle the HTML
that Google Sheets and Excel place on the clipboard when cells are copied, as well
as arbitrary non-tabular content (plain text, code, JSON, URLs).
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import ClassVar, Literal


class _TableExtractor(HTMLParser):
    """Extract rows/cells from the first <table> in an HTML fragment."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._in_table = False
        self._table_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table_depth += 1
            if not self._in_table:
                self._in_table = True
        elif self._in_table and self._table_depth == 1:
            if tag == "tr":
                self._current_row = []
            elif tag in ("td", "th"):
                self._current_cell = []
            elif tag == "br" and self._current_cell is not None:
                self._current_cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table_depth -= 1
            if self._table_depth == 0:
                self._in_table = False
        elif self._in_table and self._table_depth == 1:
            if tag in ("td", "th") and self._current_cell is not None:
                text = "".join(self._current_cell).strip()
                if self._current_row is not None:
                    self._current_row.append(text)
                self._current_cell = None
            elif tag == "tr" and self._current_row is not None:
                self.rows.append(self._current_row)
                self._current_row = None

    def handle_data(self, data: str) -> None:
        # Only collect text at the outer table's depth: a nested table inside
        # a cell would otherwise leak its inner-cell text into the outer cell
        # because _current_cell is still set while inner <td>/<th> are skipped.
        if self._current_cell is not None and self._table_depth == 1:
            self._current_cell.append(data)


def parse_html_table(html: str) -> list[list[str]]:
    """Extract the first table from an HTML string as a list of rows.

    Returns an empty list if no table is found.
    """
    parser = _TableExtractor()
    parser.feed(html)
    return parser.rows


def parse_tsv(text: str) -> list[list[str]]:
    """Parse tab-separated text into rows.

    Returns an empty list if the text doesn't appear to be tabular
    (i.e., has no tabs or only a single cell).

    Handles RFC 4180-style quoting so that fields containing embedded
    tabs or newlines are kept intact when wrapped in double quotes.
    """
    if "\t" not in text:
        return []

    rows: list[list[str]] = []
    reader = csv.reader(io.StringIO(text), delimiter="\t", quotechar='"')
    for row in reader:
        if any(cell.strip() for cell in row):
            rows.append(row)

    # Reject a single row with fewer than two non-empty cells: copying a
    # single Excel cell on Windows commonly produces "word\t" which would
    # otherwise be rendered as a misleading 1x2 table with a phantom
    # empty column.
    if len(rows) == 1 and sum(1 for c in rows[0] if c.strip()) < 2:
        return []

    return rows


# ---------------------------------------------------------------------------
# HTML-to-text extraction (for non-table HTML)
# ---------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """Strip HTML tags and extract readable text."""

    # Tags that should insert a newline when opened
    _BLOCK_TAGS: ClassVar[set[str]] = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }

    def __init__(self) -> None:
        super().__init__()
        self._pieces: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS and self._skip_depth == 0:
            self._pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._pieces.append(data)


# The Windows CF_HTML ("HTML Format") header: a "Version:<digits>.<digits>"
# line, then the documented description keys (StartHTML, EndHTML,
# StartFragment, EndFragment, StartSelection, EndSelection with numeric
# offsets, and SourceURL), before the HTML itself. At least one offset or
# SourceURL line must follow the Version line, digits are ASCII only, and
# only those shapes are matched, so ordinary HTML whose first line is
# "Version: draft" or "Version:2.0", or a body line that looks like
# "Word: value", is never stripped. Lines may end in CRLF, LF or CR.
_CF_HTML_EOL = r"(?:\r\n|\r|\n)"
_CF_HTML_HEADER = re.compile(
    r"\AVersion:\d+\.\d+" + _CF_HTML_EOL + r"(?:(?:(?:Start|End)(?:HTML|Fragment|Selection):-?\d+"
    r"|SourceURL:[^\r\n]*)" + _CF_HTML_EOL + r")+",
    re.ASCII,
)


def strip_cf_html_header(html: str) -> str:
    """Remove a Windows CF_HTML header, if present, leaving the HTML.

    Windows stores text/html with a plain-text header of byte offsets
    ahead of the document. Left in place, extract_html_text would show
    that header to the model as part of the text. HTML without the
    header is returned unchanged.
    """
    match = _CF_HTML_HEADER.match(html)
    return html[match.end() :] if match else html


def extract_html_text(html: str) -> str:
    """Strip HTML tags and return readable plain text.

    Inserts newlines at block boundaries (p, div, br, li, etc.).
    Strips script/style content. Returns empty string for empty input.
    """
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    # Collapse multiple blank lines, strip leading/trailing whitespace
    text = "".join(parser._pieces)
    lines = [line.strip() for line in text.splitlines()]
    # Remove consecutive empty lines
    result: list[str] = []
    for line in lines:
        if line or (result and result[-1]):
            result.append(line)
    return "\n".join(result).strip()


# ---------------------------------------------------------------------------
# Content-type detection (for non-tabular text)
# ---------------------------------------------------------------------------

ContentType = Literal["json", "url", "code", "text"]

# Strong code indicators: a single match at the start of a line (after any
# leading whitespace) is sufficient to classify as code. Each pattern includes
# enough syntactic context (parens, assignment, trailing EOL) to distinguish
# real code from prose that happens to start with the same keyword (#77).
_STRONG_CODE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.MULTILINE)
    for p in (
        r"^\s*def\s+\w+\s*\(",  # Python def with parens
        # Python import / from X import Y (single or comma/as list, must reach EOL)
        r"^\s*(?:from\s+[\w.]+\s+)?import\s+\w[\w.]*(?:\s*(?:,|as\s)\s*\w[\w.]*)*\s*$",
        r"^\s*function\s+\w*\s*\(",  # JS function
        r"^\s*(?:const|let|var)\s+\w+\s*=",  # JS declaration with assignment
        r"^\s*func\s+\w*\s*\(",  # Go func
        r"^\s*package\s+[\w.]+\s*$",  # Go package (must reach EOL)
        r"^\s*fn\s+\w+\s*\(",  # Rust fn
        r"^\s*pub\s+(?:fn|struct|mod|use|const|let|static)\b",  # Rust visibility
        r"^\s*mod\s+\w+\s*[{;]",  # Rust mod with body or semicolon
        r"^\s*(?:if|for|while)\s*\(",  # Control flow with parens
        r"^\s*#!/",  # Shebang
    )
)

# Weak code indicators: common in code but also appear in prose.
# Need 2+ distinct matches to classify as code.
_WEAK_CODE_PATTERNS = (
    "class ",
    "return ",  # Python (also English words)
    "public ",
    "private ",
    "protected ",  # Java/C# (also English words)
    "=>",
    "->",
    "::",
    "&&",
    "||",  # Operators (also appear in prose)
)


def detect_content_type(text: str) -> ContentType:
    """Classify plain text content as json, url, code, or text."""
    stripped = text.strip()
    if not stripped:
        return "text"

    # JSON: starts with { or [ and parses successfully
    if stripped[0] in "{[":
        try:
            json.loads(stripped)
            return "json"
        except (json.JSONDecodeError, ValueError):
            pass

    # URL: single line, starts with http:// or https://
    lines = stripped.splitlines()
    if len(lines) == 1 and (stripped.startswith("http://") or stripped.startswith("https://")):
        return "url"

    # Code: a single strong pattern is enough. Each pattern is a MULTILINE
    # regex anchored to line start that requires syntactic context (parens,
    # assignment, or trailing EOL) to distinguish real code from prose that
    # happens to start with a lowercase keyword (#68, #77).
    for pattern in _STRONG_CODE_PATTERNS:
        if pattern.search(stripped):
            return "code"

    # Code: need 2+ distinct weak patterns to avoid false positives on prose
    weak_hits = sum(1 for pattern in _WEAK_CODE_PATTERNS if pattern in stripped)
    if weak_hits >= 2:
        return "code"

    # Code: significant indentation (4+ spaces or tabs at start of lines)
    indented_lines = sum(1 for line in lines if line.startswith("    ") or line.startswith("\t"))
    if len(lines) > 2 and indented_lines / len(lines) > 0.3:
        return "code"

    return "text"


# ---------------------------------------------------------------------------
# Table schema inference
# ---------------------------------------------------------------------------

ColumnType = Literal["integer", "float", "currency", "percentage", "date", "boolean", "text"]

_BOOLEAN_VALUES = frozenset({"true", "false", "yes", "no"})
_INTEGER_RE = re.compile(r"^[+-]?(\d{1,3}(,\d{3})*|\d+)$")
_FLOAT_RE = re.compile(r"^[+-]?\d*\.\d+$|^[+-]?\d+\.\d+$")
_CURRENCY_RE = re.compile(
    r"^[£$€¥]\s*-?\d{1,3}(,\d{3})*(\.\d{1,2})?$"
    r"|^-?\d{1,3}(,\d{3})*(\.\d{1,2})?\s*[£$€¥]$"
)
_PERCENTAGE_RE = re.compile(r"^-?\d+(\.\d+)?%$")

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%m/%d/%y",
    "%d/%m/%y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
)


def _classify_cell(value: str) -> ColumnType:
    """Classify a single non-empty cell value as the most specific type."""
    v = value.strip()

    if v.lower() in _BOOLEAN_VALUES:
        return "boolean"

    # Percentage before float/integer (ends with %)
    if _PERCENTAGE_RE.match(v):
        return "percentage"

    # Currency before integer/float (starts/ends with symbol)
    if _CURRENCY_RE.match(v):
        return "currency"

    # Integer before float (no decimal point)
    if _INTEGER_RE.match(v):
        return "integer"

    if _FLOAT_RE.match(v):
        return "float"

    # Date: a date must contain at least one digit -- skip parsing for pure text
    if not any(c.isdigit() for c in v):
        return "text"

    # ISO format first, then common regional formats
    try:
        datetime.fromisoformat(v)
        return "date"
    except ValueError:
        pass

    for fmt in _DATE_FORMATS:
        try:
            datetime.strptime(v, fmt)
            return "date"
        except ValueError:
            continue

    return "text"


def infer_column_types(rows: list[list[str]]) -> list[ColumnType]:
    """Infer the data type of each column from a table.

    The first row is treated as a header and excluded from inference.
    Empty cells are skipped. Majority-wins: if more than half of the
    non-empty cells in a column match a specific type, that type is
    used; otherwise the column is typed as ``text``.

    Returns one :data:`ColumnType` per column (based on the widest row),
    or an empty list when there are fewer than 2 rows (no data to infer from).
    """
    if len(rows) < 2:
        return []

    data_rows = rows[1:]
    num_cols = max(len(row) for row in rows)

    result: list[ColumnType] = []
    for col_idx in range(num_cols):
        type_counts: dict[str, int] = {}
        non_empty = 0

        for row in data_rows:
            if col_idx >= len(row):
                continue
            cell = row[col_idx].strip()
            if not cell:
                continue
            non_empty += 1
            col_type = _classify_cell(cell)
            type_counts[col_type] = type_counts.get(col_type, 0) + 1

        if not non_empty:
            result.append("text")
            continue

        best_type = max(type_counts, key=lambda t: type_counts[t])
        if type_counts[best_type] > non_empty / 2:
            result.append(best_type)  # type: ignore[arg-type]
        else:
            result.append("text")

    return result


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

OutputFormat = Literal["markdown", "json", "csv", "slack", "jira", "confluence", "html", "notion"]


def _has_header_row(rows: list[list[str]]) -> bool:
    """Heuristic: does the first row look like a header?

    Returns True if the first row's cell types differ from the majority
    type in each column. A row of all-text values where the data is also
    all-text is NOT considered a header.
    """
    if len(rows) < 2:
        return False

    first_row_types = [_classify_cell(cell) for cell in rows[0]]
    data_types = infer_column_types(rows)

    # Header if at least one column's first-row type differs from data type
    for first_type, col_type in zip(first_row_types, data_types, strict=False):
        if first_type != col_type:
            return True

    return False


def format_table(rows: list[list[str]], fmt: OutputFormat = "markdown") -> str:
    """Format parsed rows into the requested output format.

    For JSON output, the first row is treated as a header (keys) only
    when its cell types differ from the data rows. Otherwise all rows
    are included as data.
    """
    if not rows:
        return ""

    if fmt == "json":
        max_cols = max(len(r) for r in rows)
        has_header = _has_header_row(rows)

        if len(rows) == 1:
            # Single row -- flat array of values
            result_data: list | dict = list(rows[0])
        elif max_cols == 1 and has_header:
            # Single-column with header: key -> values
            result_data = {rows[0][0]: [row[0] for row in rows[1:]]}
        elif max_cols == 1:
            # Single-column, no header: flat array of all values
            result_data = [row[0] for row in rows]
        elif has_header:
            # Multi-column with header: first row as keys
            header = rows[0]
            result_data = [dict(zip(header, row, strict=False)) for row in rows[1:]]
        else:
            # Multi-column, no header: list of lists
            result_data = [list(row) for row in rows]
        return json.dumps(result_data, indent=2, ensure_ascii=False)

    elif fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
        writer.writerows(rows)
        return buf.getvalue()

    elif fmt == "slack":
        return _format_slack(rows)

    elif fmt in ("jira", "confluence"):
        return _format_jira(rows)

    elif fmt == "html":
        return _format_html(rows)

    elif fmt == "notion":
        return _format_markdown(rows)  # Notion renders standard GFM pipe tables natively

    else:  # markdown
        return _format_markdown(rows)


def _escape_md_cell(cell: str) -> str:
    """Escape characters that break GFM pipe tables."""
    return cell.replace("\\", "\\\\").replace("|", "\\|")


def _format_markdown(rows: list[list[str]]) -> str:
    """Render rows as a GitHub-flavored Markdown table."""
    if not rows:
        return ""

    # Normalize column count
    max_cols = max(len(row) for row in rows)
    normalized = [row + [""] * (max_cols - len(row)) for row in rows]

    # Escape cells, then calculate widths from escaped values
    escaped = [[_escape_md_cell(cell) for cell in row] for row in normalized]

    widths = [max(len(escaped[r][c]) for r in range(len(escaped))) for c in range(max_cols)]
    # Minimum width of 3 for the separator
    widths = [max(w, 3) for w in widths]

    lines: list[str] = []
    # Header
    header = "| " + " | ".join(escaped[0][c].ljust(widths[c]) for c in range(max_cols)) + " |"
    lines.append(header)

    # Separator
    sep = "| " + " | ".join("-" * widths[c] for c in range(max_cols)) + " |"
    lines.append(sep)

    # Data rows
    for row in escaped[1:]:
        line = "| " + " | ".join(row[c].ljust(widths[c]) for c in range(max_cols)) + " |"
        lines.append(line)

    return "\n".join(lines)


def _format_slack(rows: list[list[str]]) -> str:
    """Render rows for Slack as a monospace code block with underlined header.

    All content (header + data) lives inside a single code block so that
    special characters (*, `, ~, etc.) are rendered literally. The header
    row is visually separated by a dashed underline.

    This avoids the escaping problems that arise when headers use *bold*
    markup outside the code block (Slack mrkdwn has no escape mechanism
    for formatting characters). See #19 and #31.
    """
    if not rows:
        return ""

    max_cols = max(len(row) for row in rows)
    normalized = [row + [""] * (max_cols - len(row)) for row in rows]

    widths = [max(len(normalized[r][c]) for r in range(len(normalized))) for c in range(max_cols)]
    widths = [max(w, 1) for w in widths]

    lines: list[str] = []

    # Header row
    header = "  ".join(normalized[0][c].ljust(widths[c]) for c in range(max_cols)).rstrip()
    lines.append(header)

    # Underline separator
    sep = "  ".join("-" * widths[c] for c in range(max_cols))
    lines.append(sep)

    # Data rows
    for row in normalized[1:]:
        lines.append("  ".join(row[c].ljust(widths[c]) for c in range(max_cols)).rstrip())

    return "```\n" + "\n".join(lines) + "\n```"


def _escape_jira_cell(cell: str) -> str:
    """Escape characters that break Jira/Confluence wiki markup tables."""
    return cell.replace("\\", "\\\\").replace("|", "\\|")


def _format_jira(rows: list[list[str]]) -> str:
    """Render rows as Jira/Confluence wiki markup: ||Header|| / |Cell| syntax."""
    if not rows:
        return ""

    max_cols = max(len(row) for row in rows)
    normalized = [row + [""] * (max_cols - len(row)) for row in rows]

    escaped = [[_escape_jira_cell(cell) for cell in row] for row in normalized]

    lines = ["||" + "||".join(escaped[0]) + "||"]
    for row in escaped[1:]:
        lines.append("|" + "|".join(row) + "|")

    return "\n".join(lines)


def _format_html(rows: list[list[str]]) -> str:
    """Render rows as an HTML table with thead/tbody."""
    if not rows:
        return ""

    max_cols = max(len(row) for row in rows)
    normalized = [row + [""] * (max_cols - len(row)) for row in rows]

    lines = ["<table>", "  <thead>"]
    lines.append(
        "    <tr>" + "".join(f"<th>{html.escape(cell)}</th>" for cell in normalized[0]) + "</tr>"
    )
    lines.append("  </thead>")
    lines.append("  <tbody>")
    for row in normalized[1:]:
        lines.append(
            "    <tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>"
        )
    lines.append("  </tbody>")
    lines.append("</table>")

    return "\n".join(lines)
