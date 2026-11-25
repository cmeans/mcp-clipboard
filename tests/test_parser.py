"""Tests for the HTML/TSV parser, formatters, and content detection."""

import re

from mcp_clipboard.parser import (
    _has_header_row,
    detect_content_type,
    extract_html_text,
    format_table,
    infer_column_types,
    parse_html_table,
    parse_tsv,
    strip_cf_html_header,
)

# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


GOOGLE_SHEETS_HTML = """
<meta charset='utf-8'>
<google-sheets-html-origin>
<style type="text/css"><!--td {border: 1px solid #ccc;}--></style>
<table xmlns="http://www.w3.org/1999/xhtml" cellspacing="0" cellpadding="0" dir="ltr"
       border="1" style="table-layout:fixed;font-size:10pt;font-family:Arial;">
  <colgroup><col width="100"/><col width="100"/><col width="100"/></colgroup>
  <tbody>
    <tr style="height:21px;">
      <td style="padding:2px 3px;" data-sheets-value='{"1":2,"2":"Name"}'>Name</td>
      <td style="padding:2px 3px;" data-sheets-value='{"1":2,"2":"Age"}'>Age</td>
      <td style="padding:2px 3px;" data-sheets-value='{"1":2,"2":"City"}'>City</td>
    </tr>
    <tr style="height:21px;">
      <td style="padding:2px 3px;" data-sheets-value='{"1":2,"2":"Alice"}'>Alice</td>
      <td style="padding:2px 3px;" data-sheets-value='{"1":3,"3":30}'>30</td>
      <td style="padding:2px 3px;" data-sheets-value='{"1":2,"2":"Portland"}'>Portland</td>
    </tr>
    <tr style="height:21px;">
      <td style="padding:2px 3px;" data-sheets-value='{"1":2,"2":"Bob"}'>Bob</td>
      <td style="padding:2px 3px;" data-sheets-value='{"1":3,"3":25}'>25</td>
      <td style="padding:2px 3px;" data-sheets-value='{"1":2,"2":"Seattle"}'>Seattle</td>
    </tr>
  </tbody>
</table>
"""


def test_parse_google_sheets_html():
    rows = parse_html_table(GOOGLE_SHEETS_HTML)
    assert len(rows) == 3
    assert rows[0] == ["Name", "Age", "City"]
    assert rows[1] == ["Alice", "30", "Portland"]
    assert rows[2] == ["Bob", "25", "Seattle"]


SIMPLE_HTML = """
<table>
<tr><th>Col A</th><th>Col B</th></tr>
<tr><td>1</td><td>2</td></tr>
</table>
"""


def test_parse_simple_html():
    rows = parse_html_table(SIMPLE_HTML)
    assert len(rows) == 2
    assert rows[0] == ["Col A", "Col B"]
    assert rows[1] == ["1", "2"]


def test_parse_html_no_table():
    rows = parse_html_table("<p>No table here</p>")
    assert rows == []


def test_parse_html_empty():
    rows = parse_html_table("")
    assert rows == []


def test_parse_html_nested_table_isolates_outer():
    """Inner-table text must not leak into the outer cell."""
    html = (
        "<table><tr><td>outer<table><tr><td>inner</td></tr></table></td><td>after</td></tr></table>"
    )
    rows = parse_html_table(html)
    assert len(rows) == 1
    assert rows[0] == ["outer", "after"]


# ---------------------------------------------------------------------------
# TSV parsing
# ---------------------------------------------------------------------------


def test_parse_tsv_basic():
    text = "Name\tAge\tCity\nAlice\t30\tPortland\nBob\t25\tSeattle"
    rows = parse_tsv(text)
    assert len(rows) == 3
    assert rows[0] == ["Name", "Age", "City"]
    assert rows[1] == ["Alice", "30", "Portland"]


def test_parse_tsv_no_tabs():
    rows = parse_tsv("just plain text with no tabs")
    assert rows == []


def test_parse_tsv_single_column():
    # No tabs means not tabular
    rows = parse_tsv("line1\nline2\nline3")
    assert rows == []


def test_parse_tsv_single_cell_trailing_tab():
    """A single non-empty cell with a trailing tab is not a table."""
    # Common output from copying one cell out of Excel on Windows.
    rows = parse_tsv("word\t")
    assert rows == []


def test_parse_tsv_single_cell_leading_tab():
    """A single non-empty cell preceded by a tab is not a table."""
    rows = parse_tsv("\tword")
    assert rows == []


def test_parse_tsv_quoted_tab():
    """A quoted field containing a tab should not split the cell."""
    text = '"has\ttab"\tplain'
    rows = parse_tsv(text)
    assert len(rows) == 1
    assert rows[0] == ["has\ttab", "plain"]


def test_parse_tsv_quoted_newline():
    """A quoted field containing a newline should keep the cell intact."""
    text = '"line1\nline2"\tother'
    rows = parse_tsv(text)
    assert len(rows) == 1
    assert rows[0] == ["line1\nline2", "other"]


def test_parse_tsv_mixed_quoted_unquoted():
    """Mix of quoted and unquoted fields."""
    text = 'Name\tDesc\nAlice\t"likes\ttabs"'
    rows = parse_tsv(text)
    assert len(rows) == 2
    assert rows[0] == ["Name", "Desc"]
    assert rows[1] == ["Alice", "likes\ttabs"]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_format_markdown():
    rows = [["Name", "Age"], ["Alice", "30"], ["Bob", "25"]]
    md = format_table(rows, "markdown")
    assert "| Name" in md
    assert "| ---" in md
    assert "| Alice" in md


def test_format_json():
    rows = [["Name", "Age"], ["Alice", "30"]]
    result = format_table(rows, "json")
    import json

    data = json.loads(result)
    assert len(data) == 1
    assert data[0]["Name"] == "Alice"
    assert data[0]["Age"] == "30"


def test_format_json_single_column_with_header():
    """Single-column with detectable header uses it as a key."""
    import json

    rows = [["Count"], ["42"], ["17"], ["99"]]
    result = format_table(rows, "json")
    data = json.loads(result)
    # "Count" is text, data is integers -> header detected
    assert data == {"Count": ["42", "17", "99"]}


def test_format_json_single_column_no_header():
    """Single-column where all rows are the same type -> flat array."""
    import json

    rows = [["Alice"], ["Bob"], ["Carol"]]
    result = format_table(rows, "json")
    data = json.loads(result)
    # All text -> no header, include all values
    assert data == ["Alice", "Bob", "Carol"]


def test_format_json_single_row():
    """Single row produces a flat array of values."""
    import json

    rows = [["Name", "Age", "City"]]
    result = format_table(rows, "json")
    data = json.loads(result)
    assert data == ["Name", "Age", "City"]


def test_format_json_no_header_detected():
    """Multi-column where first row matches data types -> list of lists."""
    import json

    rows = [["Alice", "30"], ["Bob", "25"], ["Carol", "35"]]
    result = format_table(rows, "json")
    data = json.loads(result)
    # First row types match data -> no header, all rows as data
    assert data == [["Alice", "30"], ["Bob", "25"], ["Carol", "35"]]


def test_format_csv():
    rows = [["A", "B"], ["1", "2"]]
    result = format_table(rows, "csv")
    assert '"A","B"' in result
    assert '"1","2"' in result


def test_format_csv_single_column():
    """Single-column CSV should still look like CSV (quoted values)."""
    rows = [["Address"], ["123 Main St"], ["456 Oak Ave"]]
    result = format_table(rows, "csv")
    assert '"Address"' in result
    assert '"123 Main St"' in result
    assert '"456 Oak Ave"' in result


def test_format_empty():
    assert format_table([], "markdown") == ""
    assert format_table([], "json") == ""
    assert format_table([], "csv") == ""


def test_format_ragged_rows_markdown():
    """Rows with different column counts should be padded."""
    rows = [["A", "B", "C"], ["1", "2"]]
    md = format_table(rows, "markdown")
    # The second row should be padded to 3 columns
    lines = md.strip().split("\n")
    assert len(lines) == 3  # header + separator + 1 data row
    # Count pipes in each line — should be consistent
    assert lines[0].count("|") == lines[2].count("|")


# ---------------------------------------------------------------------------
# HTML text extraction
# ---------------------------------------------------------------------------


def test_extract_html_text_paragraphs():
    html = "<p>First paragraph.</p><p>Second paragraph.</p>"
    text = extract_html_text(html)
    assert "First paragraph." in text
    assert "Second paragraph." in text
    # Paragraphs should be on separate lines
    assert "\n" in text


def test_extract_html_text_with_tags():
    html = "<b>Bold</b> and <i>italic</i> text"
    text = extract_html_text(html)
    assert text == "Bold and italic text"


def test_extract_html_text_strips_script_style():
    html = "<style>body { color: red; }</style><p>Visible</p><script>alert('x')</script>"
    text = extract_html_text(html)
    assert "Visible" in text
    assert "color" not in text
    assert "alert" not in text


def test_extract_html_text_consecutive_skip_tags():
    """Consecutive script/style tags should each be fully skipped."""
    html = "<style>css{}</style><script>js()</script><p>Visible</p>"
    text = extract_html_text(html)
    assert "Visible" in text
    assert "css" not in text
    assert "js" not in text


def test_extract_html_text_br_newlines():
    html = "Line one<br>Line two<br/>Line three"
    text = extract_html_text(html)
    assert "Line one" in text
    assert "Line two" in text
    assert "Line three" in text


def test_extract_html_text_list_items():
    html = "<ul><li>Item 1</li><li>Item 2</li><li>Item 3</li></ul>"
    text = extract_html_text(html)
    assert "Item 1" in text
    assert "Item 2" in text
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) >= 3


def test_extract_html_text_empty():
    assert extract_html_text("") == ""


# ---------------------------------------------------------------------------
# Content-type detection
# ---------------------------------------------------------------------------


def test_detect_json_object():
    assert detect_content_type('{"name": "Alice", "age": 30}') == "json"


def test_detect_json_array():
    assert detect_content_type("[1, 2, 3]") == "json"


def test_detect_json_invalid():
    """Starts with { but isn't valid JSON — should not be detected as JSON."""
    assert detect_content_type("{this is not json}") != "json"


def test_detect_url_https():
    assert detect_content_type("https://example.com/path?q=1") == "url"


def test_detect_url_http():
    assert detect_content_type("http://example.com") == "url"


def test_detect_url_multiline_not_url():
    """Multiple lines starting with URL should not be detected as url."""
    text = "https://example.com\nhttps://other.com"
    assert detect_content_type(text) != "url"


def test_detect_code_python():
    code = "def hello():\n    print('hello world')\n\nhello()"
    assert detect_content_type(code) == "code"


def test_detect_code_javascript():
    code = "const x = 42;\nfunction add(a, b) { return a + b; }"
    assert detect_content_type(code) == "code"


def test_detect_code_indented():
    """Heavily indented text should be detected as code."""
    code = "main:\n    step 1\n    step 2\n    step 3\n    step 4"
    assert detect_content_type(code) == "code"


def test_detect_plain_text():
    assert detect_content_type("Hello, this is just a plain sentence.") == "text"


def test_detect_empty():
    assert detect_content_type("") == "text"
    assert detect_content_type("   ") == "text"


def test_detect_prose_with_return():
    """Prose containing 'return ' as an English word should not be code."""
    assert detect_content_type("Please return the book to the library.") == "text"


def test_detect_prose_with_class():
    """Prose containing 'class ' as an English word should not be code."""
    assert detect_content_type("The class was canceled due to weather.") == "text"


def test_detect_prose_with_public():
    """Prose containing 'public ' should not be code."""
    assert detect_content_type("The public hearing is scheduled for Tuesday.") == "text"


def test_detect_prose_with_arrow():
    """Prose containing -> as an arrow should not be code."""
    assert detect_content_type("Step 1 -> Step 2 -> Step 3") == "text"


def test_detect_prose_with_double_pipe():
    """Prose mentioning || should not be code."""
    assert detect_content_type("Use the logical OR operator (||) in expressions.") == "text"


def test_detect_prose_with_double_colon():
    """Prose containing :: should not be code."""
    assert detect_content_type("See ref::section for more details.") == "text"


def test_detect_code_multiple_weak_signals():
    """Multiple weak signals together should still detect as code."""
    assert detect_content_type("x = a || b && c") == "code"


def test_detect_code_strong_signal():
    """A single strong signal is enough to detect code."""
    assert detect_content_type("#!/bin/bash\necho hello") == "code"
    assert detect_content_type("function add(a, b) { }") == "code"
    assert detect_content_type("if (x > 0) { }") == "code"


def test_detect_prose_with_from():
    """Prose containing 'from ' as a preposition should not be code (#68)."""
    assert detect_content_type("I got the data from the system yesterday.") == "text"


def test_detect_prose_with_import():
    """Prose containing 'import ' mid-sentence should not be code (#68)."""
    assert detect_content_type("The customs office reviews each import before release.") == "text"


def test_detect_code_python_import_at_line_start():
    """Real Python imports at line start must still be detected as code (#68)."""
    assert detect_content_type("import os") == "code"
    assert detect_content_type("from collections import defaultdict") == "code"
    assert detect_content_type("import json\nimport sys") == "code"


def test_detect_prose_starting_with_let():
    """Prose starting with lowercase 'let' must not classify as code (#77)."""
    assert detect_content_type("let me know when you are ready") == "text"


def test_detect_prose_starting_with_from():
    """Prose starting with lowercase 'from' (preposition) must not classify as code (#77)."""
    assert detect_content_type("from the desk of the president") == "text"


def test_detect_prose_starting_with_import():
    """Prose starting with lowercase 'import' (noun) must not classify as code (#77)."""
    assert detect_content_type("import tariffs affect trade") == "text"


def test_detect_prose_starting_with_def():
    """Prose starting with lowercase 'def' must not classify as code (#77)."""
    assert detect_content_type("def leaves me confused") == "text"


def test_detect_prose_starting_with_var():
    """Prose starting with lowercase 'var' must not classify as code (#77)."""
    assert detect_content_type("var is short for variable") == "text"


def test_detect_code_js_declarations_still_detected():
    """Real JS variable declarations with assignment must still classify as code (#77)."""
    assert detect_content_type("let x = 42") == "code"
    assert detect_content_type("const PI = 3.14") == "code"
    assert detect_content_type("var total = 0;") == "code"


def test_detect_code_python_def_still_detected():
    """Real Python def with parens must still classify as code (#77)."""
    assert detect_content_type("def hello():") == "code"
    assert detect_content_type("def add(a, b):") == "code"


# ---------------------------------------------------------------------------
# _has_header_row direct coverage
# ---------------------------------------------------------------------------


def test_has_header_row_single_row():
    """Single row triggers the early return; can't be a header without data."""
    assert _has_header_row([["Name", "Age", "City"]]) is False


def test_has_header_row_empty():
    """Zero rows also takes the early-return path."""
    assert _has_header_row([]) is False


def test_has_header_row_uniform_text_no_header():
    """When every row (header included) is text and data is also text, no
    column type differs, so no header is detected."""
    rows = [["Alice", "Bob"], ["Carol", "Dan"], ["Eve", "Frank"]]
    assert _has_header_row(rows) is False


def test_has_header_row_text_header_over_numeric_data():
    """Classic header case: text labels over numeric data rows."""
    rows = [["Count", "Total"], ["1", "100"], ["2", "200"], ["3", "300"]]
    assert _has_header_row(rows) is True


# ---------------------------------------------------------------------------
# Schema inference
# ---------------------------------------------------------------------------


def test_infer_integer_column():
    rows = [["Count"], ["1"], ["42"], ["1,000"], ["99"]]
    assert infer_column_types(rows) == ["integer"]


def test_infer_float_column():
    rows = [["Score"], ["3.14"], ["2.71"], ["0.5"]]
    assert infer_column_types(rows) == ["float"]


def test_infer_currency_column():
    rows = [["Price"], ["$1.99"], ["$10.00"], ["$1,234.56"]]
    assert infer_column_types(rows) == ["currency"]


def test_infer_currency_other_symbols():
    rows = [["Amount"], ["£9.99"], ["€100"], ["¥1,000"]]
    assert infer_column_types(rows) == ["currency"]


def test_infer_percentage_column():
    rows = [["Rate"], ["10%"], ["3.5%"], ["100%"]]
    assert infer_column_types(rows) == ["percentage"]


def test_infer_date_iso():
    rows = [["Date"], ["2024-01-15"], ["2024-06-30"], ["2023-12-01"]]
    assert infer_column_types(rows) == ["date"]


def test_infer_date_us_format():
    rows = [["Date"], ["01/15/2024"], ["06/30/2024"], ["12/01/2023"]]
    assert infer_column_types(rows) == ["date"]


def test_infer_date_long_format():
    rows = [["Date"], ["January 15, 2024"], ["June 30, 2024"]]
    assert infer_column_types(rows) == ["date"]


def test_infer_boolean_column():
    rows = [["Active"], ["true"], ["false"], ["true"]]
    assert infer_column_types(rows) == ["boolean"]


def test_infer_boolean_yes_no():
    rows = [["Enabled"], ["yes"], ["no"], ["yes"]]
    assert infer_column_types(rows) == ["boolean"]


def test_infer_text_column():
    rows = [["Name"], ["Alice"], ["Bob"], ["Carol"]]
    assert infer_column_types(rows) == ["text"]


def test_infer_multiple_columns():
    rows = [
        ["Name", "Age", "Salary", "Active", "Score", "Join Date"],
        ["Alice", "30", "$75,000", "true", "9.5", "2020-01-15"],
        ["Bob", "25", "$60,000", "false", "8.2", "2021-06-30"],
        ["Carol", "35", "$90,000", "true", "9.8", "2019-03-10"],
    ]
    types = infer_column_types(rows)
    assert types == ["text", "integer", "currency", "boolean", "float", "date"]


def test_infer_empty_cells_skipped():
    """Empty cells should not affect type inference."""
    rows = [["Val"], ["42"], [""], ["17"], [""]]
    assert infer_column_types(rows) == ["integer"]


def test_infer_header_excluded():
    """Header row must not influence type inference."""
    # Header looks like text, data is all integers — should infer integer
    rows = [["Count of Items"], ["10"], ["20"], ["30"]]
    assert infer_column_types(rows) == ["integer"]


def test_infer_no_data_rows():
    """Header-only table returns empty list."""
    rows = [["Name", "Age"]]
    assert infer_column_types(rows) == []


def test_infer_empty_rows():
    assert infer_column_types([]) == []


def test_infer_majority_wins():
    """Mixed column where one type wins majority."""
    rows = [["Val"], ["42"], ["99"], ["hello"], ["17"]]
    # 3 integers, 1 text → integer wins
    assert infer_column_types(rows) == ["integer"]


def test_infer_no_majority_falls_back_to_text():
    """No type wins majority → text."""
    rows = [["Val"], ["42"], ["hello"], ["3.14"], ["world"]]
    # 1 integer, 1 text, 1 float, 1 text → text has 2, float and integer have 1 each
    # text wins majority (2/4 = 50%, not strictly > 50%) → falls back to text
    assert infer_column_types(rows) == ["text"]


def test_infer_all_empty_column():
    """A column with all empty cells returns text."""
    rows = [["A", "B"], ["1", ""], ["2", ""], ["3", ""]]
    assert infer_column_types(rows) == ["integer", "text"]


def test_infer_text_no_digits_skips_date_parsing():
    """Text-only cells (no digits) should be classified as text without date parsing."""
    rows = [["Name"], ["Alice"], ["Bob"], ["Carol"]]
    assert infer_column_types(rows) == ["text"]


def test_infer_date_with_digits_still_works():
    """Date values contain digits and should still be classified correctly."""
    rows = [["Date"], ["2024-01-15"], ["March 10, 2023"], ["12/25/2024"]]
    assert infer_column_types(rows) == ["date"]


# ---------------------------------------------------------------------------
# Destination-aware formatters
# ---------------------------------------------------------------------------

_ROWS = [["Name", "Age", "City"], ["Alice", "30", "Portland"], ["Bob", "25", "Seattle"]]


def test_format_slack_header_in_code_block():
    """Header is inside the code block as plain text (not bold)."""
    result = format_table(_ROWS, "slack")
    assert result.startswith("```\n")
    assert result.endswith("\n```")
    # Header values appear without bold markup
    assert "Name" in result
    assert "*Name*" not in result


def test_format_slack_header_underline():
    """Header row is followed by a dashed underline separator."""
    result = format_table(_ROWS, "slack")
    lines = result.strip("`\n").split("\n")
    assert len(lines) >= 2
    # Second line should be dashes
    assert set(lines[1].strip()).issubset({"-", " "})


def test_format_slack_no_pipes():
    result = format_table(_ROWS, "slack")
    # Entire content (inside code block) should have no pipe characters
    code_block = result.split("```")[1]
    assert "|" not in code_block


def test_format_slack_code_block():
    result = format_table(_ROWS, "slack")
    assert "```" in result
    assert "Alice" in result
    assert "Bob" in result


def test_format_slack_special_chars_preserved():
    """Special characters are preserved literally inside the code block."""
    rows = [["Cmd", "Note"], ["a*b", "use `code`"]]
    result = format_table(rows, "slack")
    assert "a*b" in result
    assert "use `code`" in result
    # No bold markup wrapping
    assert "*Cmd*" not in result


def test_format_jira_header_syntax():
    result = format_table(_ROWS, "jira")
    assert result.startswith("||Name||")
    assert "||Age||" in result
    assert "||City||" in result


def test_format_jira_data_syntax():
    result = format_table(_ROWS, "jira")
    assert "|Alice|30|Portland|" in result
    assert "|Bob|25|Seattle|" in result


def test_format_confluence_same_as_jira():
    assert format_table(_ROWS, "confluence") == format_table(_ROWS, "jira")


def test_format_html_structure():
    result = format_table(_ROWS, "html")
    assert "<table>" in result
    assert "<thead>" in result
    assert "<th>Name</th>" in result
    assert "<th>Age</th>" in result
    assert "<tbody>" in result
    assert "<td>Alice</td>" in result
    assert "<td>30</td>" in result
    assert "</table>" in result


def test_format_html_row_count():
    result = format_table(_ROWS, "html")
    assert result.count("<tr>") == 3  # 1 header + 2 data rows


def test_format_html_escapes_angle_brackets():
    """HTML cell values with < > must be escaped to prevent XSS."""
    rows = [["Tag"], ["<script>alert('xss')</script>"]]
    result = format_table(rows, "html")
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_format_html_escapes_ampersand():
    """HTML cell values with & must be escaped."""
    rows = [["Val"], ["A&B"]]
    result = format_table(rows, "html")
    assert "A&amp;B" in result


def test_format_html_escapes_quotes():
    """HTML cell values with quotes must be escaped."""
    rows = [["Val"], ['say "hello"']]
    result = format_table(rows, "html")
    assert "say &quot;hello&quot;" in result


def test_format_notion_is_gfm():
    """Notion uses standard GFM pipe tables."""
    assert format_table(_ROWS, "notion") == format_table(_ROWS, "markdown")


def test_format_destination_empty():
    for fmt in ("slack", "jira", "confluence", "html", "notion"):
        assert format_table([], fmt) == ""


def test_format_destination_ragged_rows():
    """Ragged rows are padded with empty cells so row shape is uniform."""
    rows = [["A", "B", "C"], ["1", "2"]]

    # Slack: both data cells must appear in the output (short row wasn't dropped).
    slack = format_table(rows, "slack")
    assert "1" in slack and "2" in slack
    assert "A" in slack and "B" in slack and "C" in slack

    # Jira/Confluence: header has 3 cells, data row must also have 3 cells
    # (including a padded empty cell for the ragged row).
    # Header format: ||A||B||C||   Data format: |1|2||  (trailing empty cell)
    for fmt in ("jira", "confluence"):
        result = format_table(rows, fmt)
        lines = result.splitlines()
        assert len(lines) == 2, f"{fmt}: expected 2 rows, got {len(lines)}"
        header_cells = lines[0][2:-2].split("||")
        data_cells = lines[1][1:-1].split("|")
        assert len(header_cells) == 3, f"{fmt}: header cell count {len(header_cells)}"
        assert len(data_cells) == 3, (
            f"{fmt}: data cell count {len(data_cells)} (short row not padded)"
        )

    # HTML: every <tr> must contain the same number of cells (<th> for header,
    # <td> for data), proving the short data row was padded.
    html = format_table(rows, "html")
    assert html.count("<th>") == 3
    # Count <td> occurrences in the single data row (only one data <tr>).
    assert html.count("<td>") == 3, f"html: expected 3 <td>, got {html.count('<td>')}"


# ---------------------------------------------------------------------------
# Markdown/Notion escaping (#16)
# ---------------------------------------------------------------------------


def test_format_markdown_escapes_pipe_in_data():
    """Pipe in a data cell must not break column structure."""
    rows = [["Cmd", "Desc"], ["cat | grep", "filter"]]
    result = format_table(rows, "markdown")
    lines = result.strip().split("\n")
    # Header + separator + 1 data row
    assert len(lines) == 3
    # Every line must have the same number of unescaped pipe delimiters
    # (escaped pipes \| do not count as delimiters)
    # 2-column table: | col1 | col2 | = 3 unescaped pipes per line
    for line in lines:
        # Count unescaped pipes: pipes not preceded by backslash
        delimiters = len(re.findall(r"(?<!\\)\|", line))
        assert delimiters == 3, f"Expected 3 pipe delimiters, got {delimiters} in: {line}"
    # The escaped pipe must appear in the output
    assert r"cat \| grep" in result


def test_format_markdown_escapes_pipe_in_header():
    """Pipe in a header cell must not break column structure."""
    rows = [["A|B", "C"], ["1", "2"]]
    result = format_table(rows, "markdown")
    lines = result.strip().split("\n")
    assert len(lines) == 3
    assert r"A\|B" in result


def test_format_markdown_escapes_double_pipe():
    """Double pipe || must not create extra columns."""
    rows = [["X", "Y"], ["a||b", "c"]]
    result = format_table(rows, "markdown")
    for line in result.strip().split("\n"):
        delimiters = len(re.findall(r"(?<!\\)\|", line))
        assert delimiters == 3  # 2-column table: | col1 | col2 | = 3 unescaped pipes


def test_format_markdown_escapes_backslash():
    r"""Backslash must be escaped so \| in source doesn't become a bare pipe."""
    rows = [["Val"], [r"a\b"]]
    result = format_table(rows, "markdown")
    assert r"a\\b" in result


def test_format_markdown_escapes_backslash_pipe():
    r"""Source \| must become \\| (escaped backslash + escaped pipe)."""
    rows = [["Val"], [r"a\|b"]]
    result = format_table(rows, "markdown")
    assert r"a\\\|b" in result


def test_format_markdown_leading_trailing_pipe():
    """Leading/trailing pipes in cell values must be escaped."""
    rows = [["Col"], ["|leading"], ["trailing|"], ["|both|"]]
    result = format_table(rows, "markdown")
    lines = result.strip().split("\n")
    # header + sep + 3 data rows = 5 lines
    assert len(lines) == 5
    for line in lines:
        delimiters = len(re.findall(r"(?<!\\)\|", line))
        assert delimiters == 2  # single column: | cell |
    assert r"\|leading" in result
    assert r"trailing\|" in result
    assert r"\|both\|" in result


def test_format_notion_escapes_pipe():
    """Notion uses _format_markdown, so escaping must apply there too."""
    rows = [["A", "B"], ["x|y", "z"]]
    md_result = format_table(rows, "markdown")
    notion_result = format_table(rows, "notion")
    assert md_result == notion_result
    assert r"x\|y" in notion_result


# ---------------------------------------------------------------------------
# Jira/Confluence escaping (#17)
# ---------------------------------------------------------------------------


def test_format_jira_escapes_pipe_in_data():
    """Pipe in a data cell must not break cell boundaries."""
    rows = [["Cmd", "Desc"], ["cat | grep", "filter"]]
    result = format_table(rows, "jira")
    lines = result.strip().split("\n")
    assert len(lines) == 2  # 1 header + 1 data row
    # Header line has 3 cells: ||Cmd||Desc||
    assert lines[0].count("||") == 3  # opening + between + closing = 3 occurrences for 2 columns
    # Data line: |cat \| grep|filter|
    assert r"cat \| grep" in result


def test_format_jira_escapes_pipe_in_header():
    """Pipe in a header cell must not create extra columns."""
    rows = [["A|B", "C"], ["1", "2"]]
    result = format_table(rows, "jira")
    lines = result.strip().split("\n")
    assert len(lines) == 2
    assert r"A\|B" in result


def test_format_jira_escapes_double_pipe():
    """|| in a data cell must not create accidental header markup."""
    rows = [["X", "Y"], ["a||b", "c"]]
    result = format_table(rows, "jira")
    lines = result.strip().split("\n")
    # Data row should still have exactly 2 cells
    data_line = lines[1]
    # The || should be escaped to \|\|
    assert r"a\|\|b" in data_line


def test_format_jira_escapes_backslash():
    r"""Backslash must be escaped to avoid swallowing the next character."""
    rows = [["Val"], [r"a\b"]]
    result = format_table(rows, "jira")
    assert r"a\\b" in result


def test_format_jira_leading_trailing_pipe():
    """Leading/trailing pipes in cell values must be escaped."""
    rows = [["Col"], ["|leading"], ["trailing|"]]
    result = format_table(rows, "jira")
    lines = result.strip().split("\n")
    assert len(lines) == 3  # 1 header + 2 data rows
    assert r"\|leading" in result
    assert r"trailing\|" in result


def test_format_confluence_escapes_same_as_jira():
    """Confluence uses _format_jira, so escaping must apply."""
    rows = [["A"], ["x|y"]]
    assert format_table(rows, "confluence") == format_table(rows, "jira")
    assert r"x\|y" in format_table(rows, "confluence")


# ---------------------------------------------------------------------------
# CF_HTML header stripping
# ---------------------------------------------------------------------------


def test_strip_cf_html_header_removes_header():
    from mcp_clipboard.clipboard import _windows_html_clipboard_wrap

    wrapped = _windows_html_clipboard_wrap("<p>caf\u00e9 \U0001f600</p>")
    assert strip_cf_html_header(wrapped) == (
        "<html><body><!--StartFragment--><p>caf\u00e9 \U0001f600</p><!--EndFragment--></body></html>"
    )


def test_strip_cf_html_header_with_source_url_and_lf_endings():
    raw = (
        "Version:1.0\nStartHTML:0000000150\nEndHTML:0000000200\n"
        "StartFragment:0000000160\nEndFragment:0000000190\n"
        "SourceURL:https://example.com/a?b=c\n<html><body>x</body></html>"
    )
    assert strip_cf_html_header(raw) == "<html><body>x</body></html>"


def test_strip_cf_html_header_leaves_plain_html_alone():
    for html in ("<p>Version: 2</p>", "", "Version:1.0", "text\nVersion:1.0\n<p>x</p>"):
        assert strip_cf_html_header(html) == html


def test_strip_cf_html_header_cr_only_line_endings():
    raw = "Version:0.9\rStartHTML:1\rEndHTML:2\r<html>x</html>"
    assert strip_cf_html_header(raw) == "<html>x</html>"


def test_strip_cf_html_header_keeps_key_value_shaped_body_line():
    raw = "Version:0.9\r\nStartHTML:1\r\nNote: total 5\r\n<b>x</b>"
    assert strip_cf_html_header(raw) == "Note: total 5\r\n<b>x</b>"
