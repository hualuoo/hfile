"""The layout of the driver modules: every module of drivers.DRIVERS, and example.py, is written the way
docs/development.md (添加存储源, 驱动的写法) says and example.py shows, so that a reader of one driver knows where to
look in the others.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_layout

Checked, for each module:

- The docstring: a first paragraph (or a few), then sections, each a heading line at column 0 with its text indented
  below it. The standard headings come in the order of HEADINGS; a topic of its own gets a heading of its own; the old
  names in HEADING_SYNONYMS and paragraphs that start with a heading ("Paths. The ...") aren't used any more.
- The imports: all at the top, grouped as __future__, standard library, third-party packages, Django, this project;
  the names of _cloud imported (from ._cloud import ...), not the module.
- The order of the module: helper functions and classes before the driver classes.
- Each driver class:
  - its class attributes in the order of ATTRIBUTES, before the first method;
  - when it derives from StorageDriver or IdDriver itself, all seven capability flags written out, and a comment line
    above each group of ClassVars (a single one may have its comment at the end of its line);
  - __init__ first, close() right after it;
  - the methods of the admin form, of files and IdDriver's hooks in the orders of FORM_METHODS, FILE_METHODS (or
    SPLIT_METHODS, with a reading and a writing section) and HOOKS, the admin form's before the others;
  - section comments ("    # -- name"): the standard ones of SECTIONS in that order (sections of the driver's own
    go after requests and before the file methods), without trailing dashes; the methods of files in the sections
    of files, the admin form's in its section;
  - in the sections of files and IdDriver's hooks, a helper method above the first method that uses it.

A rule that is wrong for a new driver can be changed here, together with docs/development.md.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

from apps.storages.drivers import DRIVERS

DRIVERS_DIR = Path(__file__).resolve().parent.parent / "drivers"

HEADINGS = ["Paths", "Login", "Requests", "Changes", "Downloads", "Uploads", "Names", "Limits"]
HEADING_SYNONYMS = {
    "Authorization": "Login", "Tokens": "Login", "Token": "Login",
    "Answers": "Requests", "Calls": "Requests",
    "Listings": "Paths", "Tree": "Paths", "The tree": "Paths",
    "Uploads (write)": "Uploads", "Writing": "Uploads",
    "Renames, moves, copies": "Changes", "Limits and rules": "Limits",
}
RUN_IN_HEADING = re.compile(r"^(?:%s|Params)(?: \([^)]*\))?[.:] \S" % "|".join(HEADINGS))

FLAGS = ["can_range", "can_direct_download", "can_direct_upload", "can_move", "can_copy", "can_search", "has_folders"]
ATTRIBUTES = ["type", "label", "service", "root_id", "params", *FLAGS, "can_write", "can_upload", "upload_conflict",
              "forbidden_chars", "max_name_bytes", "oauth", "helpers", "setup_params", "auth_params"]
ROOTS = {"StorageDriver", "IdDriver"}

FORM_METHODS = ["validate_config", "authorize_url", "exchange_code", "helper"]
FILE_METHODS = ["check", "list", "stat", "open", "open_known", "download_url", "upload_target", "write", "mkdir",
                "delete", "rename", "move", "copy", "search"]
SPLIT_METHODS = ["check", "list", "stat", "open", "open_known", "download_url", "search", "upload_target", "write",
                 "mkdir", "delete", "rename", "move", "copy"]
HOOKS = ["_account_root", "_fetch", "_create_node", "_delete_node", "_rename_node", "_move_node", "_copy_node",
         "_upload_node", "_read_node", "_cacheable"]

SECTIONS = ["the admin form", "login", "requests", "files", "reading", "writing", "IdDriver's hooks"]
SECTION_RANK = {"the admin form": 0, "login": 1, "requests": 2, "files": 4, "reading": 4, "writing": 5,
                "IdDriver's hooks": 6}  # the driver's own sections: 3
SECTION_SYNONYMS = {"authorization": "login", "tokens": "login", "token": "login", "config": "the admin form",
                    "the API": "requests", "calls": "requests"}
FILE_SECTIONS = ("files", "reading", "writing", "IdDriver's hooks")
SECTION_LINE = re.compile(r"^    # -- (.*?)\s*$")


def modules() -> list[str]:
    """The file names of the modules of drivers.DRIVERS, and example.py."""
    return sorted({spec.partition(":")[0].rpartition(".")[2] + ".py" for spec in DRIVERS.values()} | {"example.py"})


def check(source: str) -> list[str]:
    """The ways the module `source` departs from the layout, one line each."""
    tree = ast.parse(source)
    lines = source.split("\n")
    problems = _docstring(ast.get_docstring(tree, clean=False)) + _imports(tree)
    drivers: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = {ast.unparse(base) for base in node.bases}
            if bases & (ROOTS | drivers) or any(base.endswith("Driver") for base in bases):
                drivers.add(node.name)
    seen_driver = False
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in drivers:
            seen_driver = True
            problems += _class(node, lines, direct=bool({ast.unparse(base) for base in node.bases} & ROOTS))
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and seen_driver:
            problems.append(f"{node.name} (line {node.lineno}): a helper below a driver class; helpers go above them")
    return problems


def _docstring(doc: str | None) -> list[str]:
    if not doc:
        return ["the module has no docstring"]
    problems, headings = [], []
    lines = doc.split("\n")
    for i, line in enumerate(lines):
        if not line.strip() or line.startswith(" "):
            continue
        following = next((other for other in lines[i + 1:] if other.strip()), "")
        if following.startswith("    ") and not line.rstrip().endswith((".", ":", ",", ";")) and len(line) <= 50:
            name = line.strip()
            headings.append(name)
            if name in HEADING_SYNONYMS:
                problems.append(f"docstring: the heading {name!r} is {HEADING_SYNONYMS[name]!r} now")
        elif headings:
            problems.append(f"docstring: text at column 0 after the first heading, outside a section: {line[:60]!r}")
        if RUN_IN_HEADING.match(line):
            problems.append(f"docstring: a paragraph that starts with a heading; make the heading a line of its own: "
                            f"{line[:60]!r}")
    standard = [name for name in headings if name in HEADINGS]
    if standard != sorted(standard, key=HEADINGS.index):
        problems.append(f"docstring: the standard headings out of order: {standard}, not as in {HEADINGS}")
    if len(standard) != len(set(standard)):
        problems.append(f"docstring: a heading twice: {standard}")
    return problems


def _import_group(node: ast.Import | ast.ImportFrom) -> int:
    if isinstance(node, ast.ImportFrom) and node.module == "__future__":
        return 0
    if isinstance(node, ast.ImportFrom) and node.level:
        return 4
    top = (node.module if isinstance(node, ast.ImportFrom) else node.names[0].name).split(".")[0]
    if top == "django":
        return 3
    return 1 if top in sys.stdlib_module_names else 2


def _imports(tree: ast.Module) -> list[str]:
    problems = []
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    groups = [_import_group(node) for node in imports]
    if groups != sorted(groups):
        problems.append("imports: not grouped as __future__, standard library, third-party, Django, this project")
    for node in imports:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None and \
                any(alias.name == "_cloud" for alias in node.names):
            problems.append("imports: `from . import _cloud`; import its names instead (from ._cloud import ...)")
    code = next((i for i, node in enumerate(tree.body) if not isinstance(node, (ast.Import, ast.ImportFrom, ast.Expr))),
                len(tree.body))
    late = [node for node in tree.body[code:] if isinstance(node, (ast.Import, ast.ImportFrom))]
    if late:
        problems.append(f"imports: below other code (line {late[0].lineno}); they go at the top")
    return problems


def _assigned(node: ast.stmt) -> str | None:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _in_order(names: list[str], order: list[str]) -> bool:
    known = [name for name in names if name in order]
    return known == sorted(known, key=order.index)


def _class(node: ast.ClassDef, lines: list[str], *, direct: bool) -> list[str]:
    where = f"class {node.name}"
    body = node.body[1:] if node.body and isinstance(node.body[0], ast.Expr) else node.body
    problems = _attributes(where, body, lines, direct=direct)
    methods = [member for member in body if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))]
    names = [method.name for method in methods]
    if "__init__" in names and names[0] != "__init__":
        problems.append(f"{where}: __init__ isn't the first method")
    if "close" in names and names.index("close") != (names.index("__init__") + 1 if "__init__" in names else 0):
        problems.append(f"{where}: close() isn't right after __init__()")
    if not (_in_order(names, FILE_METHODS) or _in_order(names, SPLIT_METHODS)):
        problems.append(f"{where}: the file methods out of order: {[n for n in names if n in FILE_METHODS]}, "
                        f"not as in {FILE_METHODS}")
    if not _in_order(names, FORM_METHODS):
        problems.append(f"{where}: the admin form's methods out of order: {[n for n in names if n in FORM_METHODS]}")
    if not _in_order(names, HOOKS):
        problems.append(f"{where}: IdDriver's hooks out of order: {[n for n in names if n in HOOKS]}, "
                        f"not as in {HOOKS}")
    form = [i for i, name in enumerate(names) if name in FORM_METHODS]
    files = [i for i, name in enumerate(names) if name in FILE_METHODS]
    if form and files and max(form) > min(files):
        problems.append(f"{where}: a method of the admin form below a file method")
    return problems + _sections(where, node, methods, lines)


def _attributes(where: str, body: list[ast.stmt], lines: list[str], *, direct: bool) -> list[str]:
    problems = []
    assigned = [(_assigned(member), member) for member in body]
    names = [name for name, _member in assigned if name]
    if not _in_order(names, ATTRIBUTES):
        problems.append(f"{where}: class attributes out of order: {[n for n in names if n in ATTRIBUTES]}, "
                        f"not as in {ATTRIBUTES}")
    if "type" in names and names[0] != "type":
        problems.append(f"{where}: `type` isn't its first statement")
    first_method = next((i for i, member in enumerate(body) if isinstance(member, (ast.FunctionDef,
                                                                                   ast.AsyncFunctionDef))), len(body))
    late = [name for name, member in assigned[first_method:] if name]
    if late:
        problems.append(f"{where}: class attributes below the first method: {late}")
    if not direct:
        return problems
    missing = [flag for flag in FLAGS if flag not in names]
    if missing:
        problems.append(f"{where}: the capability flags aren't all written out; missing: {missing}")
    # ClassVars: a comment line above each group (no blank line inside a group); a single one may have its comment
    # at the end of its line.
    groups: list[list[ast.stmt]] = []
    for name, member in assigned:
        if not (isinstance(member, ast.AnnAssign) and "ClassVar" in ast.unparse(member.annotation)
                and name not in ATTRIBUTES):
            groups.append([])
        elif groups and groups[-1] and all(line.strip() for line in lines[groups[-1][-1].end_lineno:member.lineno - 1]):
            groups[-1].append(member)
        else:
            groups.append([member])
    for group in filter(None, groups):
        first = group[0]
        if not lines[first.lineno - 2].strip().startswith("#") and \
                not (len(group) == 1 and "  # " in lines[first.end_lineno - 1]):
            problems.append(f"{where}: no comment line above the ClassVars from {_assigned(first)} (line "
                            f"{first.lineno}) that says what they are for")
    return problems


def _sections(where: str, node: ast.ClassDef, methods: list[ast.stmt], lines: list[str]) -> list[str]:
    problems = []
    sections: list[tuple[int, str]] = []
    for lineno in range(node.lineno, node.end_lineno + 1):
        match = SECTION_LINE.match(lines[lineno - 1])
        if not match:
            continue
        title = match.group(1)
        if title.endswith("-"):
            problems.append(f"{where}: a section comment with trailing dashes (line {lineno})")
            title = title.rstrip("- ")
        if title in SECTION_SYNONYMS:
            problems.append(f"{where}: the section {title!r} is {SECTION_SYNONYMS[title]!r} now")
        sections.append((lineno, title))
    titles = [title for _lineno, title in sections]
    if [SECTION_RANK.get(t, 3) for t in titles] != sorted(SECTION_RANK.get(t, 3) for t in titles):
        problems.append(f"{where}: sections out of order: {titles}; the standard ones go as in {SECTIONS}, "
                        f"the driver's own after requests")
    standard = [title for title in titles if title in SECTIONS]
    if len(standard) != len(set(standard)):
        problems.append(f"{where}: a section twice: {titles}")

    def section_of(lineno: int) -> str | None:
        return next((title for start, title in reversed(sections) if start < lineno), None)

    for method in methods:
        section = section_of(method.lineno)
        if method.name in FILE_METHODS and section in SECTIONS and section not in FILE_SECTIONS[:3]:
            problems.append(f"{where}: {method.name}() is in the section {section!r}, not with the file methods")
        if method.name in FORM_METHODS and section not in ("the admin form", None):
            problems.append(f"{where}: {method.name}() is in the section {section!r}, not 'the admin form'")
    # In the sections of files and hooks, a helper above the first method that uses it (__init__ and close aside).
    start = min((lineno for lineno, title in sections if title in FILE_SECTIONS), default=None)
    if start is None:
        return problems
    reached = [{n.attr for n in ast.walk(method) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
               for method in methods]
    for i, method in enumerate(methods):
        name = method.name
        if not name.startswith("_") or name.startswith("__") or name in HOOKS or method.lineno < start:
            continue
        users = [j for j, other in enumerate(methods)
                 if j != i and other.name not in ("__init__", "close") and name in reached[j]]
        if users and min(users) < i:
            problems.append(f"{where}: {name}() is below {methods[min(users)].name}(), which uses it; "
                            f"a helper goes above its first user")
    return problems


class DriverLayoutTests(unittest.TestCase):
    def test_every_driver_module(self):
        names = modules()
        self.assertIn("local.py", names)
        for name in names:
            with self.subTest(module=name):
                problems = check((DRIVERS_DIR / name).read_text(encoding="utf-8"))
                self.assertFalse(problems, f"\n{name} isn't laid out as docs/development.md says:\n    "
                                           + "\n    ".join(problems))

    def test_the_check_finds_what_it_should(self):
        source = '''"""A made-up driver, laid out wrongly.

Tokens
    How they are made.

Requests
    How they are sent.

Paths
    Where things are.
Names: no slashes.
"""

from __future__ import annotations

import httpx
import json
from typing import ClassVar

from . import _cloud
from .base import StorageDriver


class BadDriver(StorageDriver):
    label = "Bad"
    type = "bad"
    can_range = True
    page_size: ClassVar[int] = 10

    def close(self):
        pass

    def __init__(self, config):
        pass

    # -- files -------------------------------------------------------------

    def write(self, path, data, size):
        self._put(path)

    def _put(self, path):
        pass

    def list(self, path):
        return []

    # -- the admin form

    @classmethod
    def validate_config(cls, config):
        return {}


def _late():
    pass
'''
        problems = "\n".join(check(source))
        for expected in ("the heading 'Tokens' is 'Login' now", "the standard headings out of order",
                         "text at column 0 after the first heading", "a paragraph that starts with a heading",
                         "imports: not grouped", "`from . import _cloud`", "class attributes out of order",
                         "`type` isn't its first statement", "the capability flags aren't all written out",
                         "no comment line above the ClassVars from page_size", "__init__ isn't the first method",
                         "close() isn't right after __init__()", "the file methods out of order",
                         "a method of the admin form below a file method", "trailing dashes",
                         "sections out of order", "_put() is below write()", "_late (line"):
            with self.subTest(expected=expected):
                self.assertIn(expected, problems)
        self.assertEqual(check((DRIVERS_DIR / "example.py").read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
