#!/usr/bin/env python3
"""
check_lookup_names.py — audit every scraper's hardcoded lookup-table name
literals against the real values in the database.

Category/type names used by scrapers (functions, jobtype, officelocations,
company_type, companysitetype) are matched with an exact
`WHERE name = %s` (or equivalent) lookup. A name that doesn't exist in the
table fails that lookup SILENTLY — no error, no distinct warning — and
whatever fallback the caller has (usually "default to Other", or leave the
field NULL) kicks in, indistinguishable from a genuine no-keyword-match.
This is exactly how a typo like 'Healthcare Provider' (real name:
'Healthcare') or 'Administration' (real name: 'Administrative') can sit
undetected in a scraper indefinitely.

This script statically scans every .py file in the repo for name literals
that feed one of these lookups — via a `*_FUNCTION_KEYWORDS`-style dict's
keys, a literal string passed to `cursor.execute(...)`, or a literal passed
to a known keyword argument (`company_type_name=`, `site_type_name=`) — and,
when run with --check-db, diffs the full set against the live database.

Usage:
    python check_lookup_names.py                # static scan only, no DB needed
    python check_lookup_names.py --check-db      # also diff against the live DB
                                                  # (must run on the production
                                                  # server — see CLAUDE.md)

This is a static-analysis heuristic, not a compiler: it will miss a name
built dynamically at runtime (e.g. string concatenation from a variable) and
can't verify anything for tables it doesn't know about. Treat "no mismatches
found" as "nothing obviously wrong", not a formal guarantee.
"""

import ast
import os
import re
import sys
from pathlib import Path
from collections import defaultdict

REPO_ROOT = Path(__file__).resolve().parent

# Each entry describes how names for that table tend to show up in scraper
# source code, based on the patterns already observed across this repo.
LOOKUP_TABLES = {
    'functions': {
        'sql_pattern': re.compile(r'from\s+functions\s+where\s+name\s*=\s*%s', re.IGNORECASE),
        'dict_name_pattern': re.compile(r'function_keywords', re.IGNORECASE),
        'kwarg_names': set(),
    },
    'jobtype': {
        'sql_pattern': re.compile(r'from\s+jobtype\s+where\s+name\s*=\s*%s', re.IGNORECASE),
        'dict_name_pattern': None,
        'kwarg_names': set(),
    },
    'officelocations': {
        'sql_pattern': re.compile(
            r'from\s+officelocations\s+where\s+(?:lower\()?name\)?\s*=\s*(?:lower\()?%s',
            re.IGNORECASE,
        ),
        'dict_name_pattern': None,
        'kwarg_names': set(),
    },
    'company_type': {
        'sql_pattern': re.compile(r'from\s+company_type\s+where\s+name\s*=\s*%s', re.IGNORECASE),
        'dict_name_pattern': None,
        'kwarg_names': {'company_type_name'},
    },
    'companysitetype': {
        'sql_pattern': re.compile(r'from\s+companysitetype\s+where\s+name\s*=\s*%s', re.IGNORECASE),
        'dict_name_pattern': None,
        'kwarg_names': {'site_type_name'},
    },
}

SKIP_DIR_NAMES = {'.git', '__pycache__', 'logs', 'node_modules'}


def _iter_python_files():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for fname in filenames:
            if fname.endswith('.py'):
                yield Path(dirpath) / fname


def _const_str(node):
    """Return the string value of an ast.Constant if it's a plain str, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class _FileScanner(ast.NodeVisitor):
    """Walks one file's AST collecting candidate lookup-table name literals.
    Walking the whole tree (not just module level) catches both module-level
    dicts (`_FUNCTION_KEYWORDS = {...}`) and dicts assigned inside a method
    body (some older scrapers build the dict locally)."""

    def __init__(self):
        self.found = defaultdict(list)  # table -> list of (name, lineno)

    def visit_Assign(self, node):
        if isinstance(node.value, ast.Dict):
            for target in node.targets:
                var_name = getattr(target, 'id', None)
                if not var_name:
                    continue
                for table, cfg in LOOKUP_TABLES.items():
                    pattern = cfg['dict_name_pattern']
                    if pattern and pattern.search(var_name):
                        for key in node.value.keys:
                            s = _const_str(key)
                            if s:
                                self.found[table].append((s, node.lineno))
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        is_execute = isinstance(func, ast.Attribute) and func.attr == 'execute'
        if is_execute and node.args:
            sql_text = self._resolve_string(node.args[0])
            if sql_text:
                for table, cfg in LOOKUP_TABLES.items():
                    if cfg['sql_pattern'].search(sql_text) and len(node.args) > 1:
                        literal = self._first_literal_in_container(node.args[1])
                        if literal:
                            self.found[table].append((literal, node.lineno))

        for kw in node.keywords:
            if not kw.arg:
                continue
            for table, cfg in LOOKUP_TABLES.items():
                if kw.arg in cfg['kwarg_names']:
                    s = _const_str(kw.value)
                    if s:
                        self.found[table].append((s, node.lineno))

        self.generic_visit(node)

    def _resolve_string(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):  # f-string: join constant parts only
            parts = [v.value for v in node.values
                     if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            return ' '.join(parts) if parts else None
        return None

    def _first_literal_in_container(self, node):
        if isinstance(node, (ast.Tuple, ast.List)) and node.elts:
            return _const_str(node.elts[0])
        return _const_str(node)


def scan_repo():
    """Returns (found, errors):
    found  -- {table: {name: {"file:line", ...}}}
    errors -- list of "file: message" strings for files that failed to parse
    """
    found = defaultdict(lambda: defaultdict(set))
    errors = []

    for path in _iter_python_files():
        try:
            source = path.read_text(encoding='utf-8-sig', errors='replace')
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as e:
            errors.append(f"{path.relative_to(REPO_ROOT)}: {e}")
            continue

        scanner = _FileScanner()
        scanner.visit(tree)
        rel_path = path.relative_to(REPO_ROOT)
        for table, entries in scanner.found.items():
            for name, lineno in entries:
                found[table][name].add(f"{rel_path}:{lineno}")

    return found, errors


def fetch_live_values(tables):
    from utils.db_connection import get_database_connection, close_connection

    conn = get_database_connection()
    live = {}
    try:
        with conn.cursor() as cursor:
            for table in tables:
                cursor.execute(f"SELECT name FROM {table} ORDER BY name")
                live[table] = {row['name'] for row in cursor.fetchall()}
    finally:
        close_connection(conn)
    return live


def main():
    check_db = '--check-db' in sys.argv

    print("=" * 78)
    print("LOOKUP NAME AUDIT")
    print("Scanning repo for hardcoded lookup-table name literals...")
    print("=" * 78)

    found, errors = scan_repo()

    if errors:
        print("\nFiles skipped due to syntax errors:")
        for e in errors:
            print(f"  {e}")

    live_values = {}
    if check_db:
        print("\nConnecting to database to fetch real lookup values...")
        live_values = fetch_live_values(LOOKUP_TABLES.keys())

    total_mismatches = 0

    for table in LOOKUP_TABLES:
        names = found.get(table, {})
        print(f"\n{'-' * 78}")
        print(f"TABLE: {table}   ({len(names)} distinct name(s) referenced in code)")
        print('-' * 78)

        if not names:
            print("  (none found)")
            continue

        real = live_values.get(table)

        for name in sorted(names):
            locations = sorted(names[name])
            if real is None:
                marker = "?"
            elif name in real:
                marker = "OK"
            else:
                marker = "MISMATCH"
                total_mismatches += 1
            loc_str = ", ".join(locations[:3])
            if len(locations) > 3:
                loc_str += f", +{len(locations) - 3} more"
            print(f"  [{marker:<8}] {name!r:<35} {loc_str}")

        if real is not None:
            unused = real - set(names.keys())
            if unused:
                print(f"  (real {table} values never referenced by any scraper: "
                      f"{', '.join(sorted(unused))})")

    print(f"\n{'=' * 78}")
    if check_db:
        if total_mismatches:
            print(f"RESULT: {total_mismatches} name(s) referenced in code do NOT exist in the database.")
        else:
            print("RESULT: every referenced name exists in the database.")
    else:
        print("Static scan only — re-run with --check-db (on the production server, "
              "where the DB is reachable) to diff these against the real tables.")
    print("=" * 78)

    return 1 if (check_db and total_mismatches) else 0


if __name__ == "__main__":
    exit(main())
