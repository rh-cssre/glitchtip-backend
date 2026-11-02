"""
SQL and description parameterization for span grouping.

Replaces literal values with %s so semantically identical queries
group together (e.g. SELECT * FROM t WHERE id = 1 and id = 2
both become SELECT * FROM t WHERE id = %s).
"""

import re

MAX_DESCRIPTION_LENGTH = 500

# Match single-quoted strings (including escaped quotes inside)
_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'")

# Match numeric literals (integers and floats) that aren't part of identifiers
_NUMERIC_LITERAL_RE = re.compile(r"\b\d+(?:\.\d+)?\b")

# Collapse IN (%s, %s, ...) → IN (%s)
_IN_COLLAPSE_RE = re.compile(r"IN\s*\(\s*(%s(?:\s*,\s*%s)*)\s*\)", re.IGNORECASE)

# Match numeric path segments: /foo/123/bar → /foo/%s/bar
_NUMERIC_PATH_RE = re.compile(r"/\d+(?=/|$)")

# Match UUID-like path segments
_UUID_PATH_RE = re.compile(
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=/|$)",
    re.IGNORECASE,
)

# Match hex hash path segments (32+ chars)
_HEX_PATH_RE = re.compile(r"/[0-9a-f]{32,}(?=/|$)", re.IGNORECASE)


def parameterize_sql(desc: str) -> str:
    """Replace string and numeric literals in SQL with %s."""
    result = _STRING_LITERAL_RE.sub("%s", desc)
    result = _NUMERIC_LITERAL_RE.sub("%s", result)
    result = _IN_COLLAPSE_RE.sub("IN (%s)", result)
    return result


def _parameterize_url(desc: str) -> str:
    """Replace variable path segments in URLs with %s."""
    # Strip query string and fragment for grouping
    base = desc.split("?")[0].split("#")[0]
    result = _UUID_PATH_RE.sub("/%s", base)
    result = _HEX_PATH_RE.sub("/%s", result)
    result = _NUMERIC_PATH_RE.sub("/%s", result)
    return result


def parameterize_description(op: str, desc: str | None) -> str:
    """Route to the appropriate parameterizer based on span operation."""
    if not desc:
        return ""

    if op.startswith("db"):
        result = parameterize_sql(desc)
    elif op.startswith("http"):
        result = _parameterize_url(desc)
    else:
        # Basic numeric replacement for other ops
        result = _NUMERIC_PATH_RE.sub("/%s", desc)

    return result[:MAX_DESCRIPTION_LENGTH]
