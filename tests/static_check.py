"""Static hygiene check: unused imports and leftover debug markers.

Run with any Python 3.10+::

    <python> tests/static_check.py

Exits non-zero when it finds something worth fixing.  Deliberately lightweight
(no third-party dependencies) so it can run anywhere the pipeline does.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Names that are legitimately imported for re-export or for typing only.
ALLOW = {
    "annotations",
    "__future__",
}

#: Files where an import exists only to keep a public API available.
REEXPORT_HINTS = ("__init__.py", "base_provider.py")

#: Files that legitimately contain the patterns below, or that exist to emit
#: console output (test fixtures, this checker itself).  Every ``probe_*.py``
#: belongs here: a probe's entire purpose is to print what it measured.
SKIP_DEBUG_SCAN = {
    "static_check.py",          # the patterns are defined here
    "make_e2e_scenes.py",       # a generated Blender fixture script reports progress
    "harness.py",               # the test harness prints results
    "run_blender_tests.py",
    "verify_end_to_end.py",
    "probe_axes.py",
    "probe_all_sequences.py",
    "probe_animation_only_equivalence.py",
    "probe_anchor_drift.py",
    "probe_bake_math.py",
    "probe_blend_size.py",
    "probe_baked_curves.py",
    "probe_camera_constraints.py",
    "probe_engine_switch.py",
    "probe_render_cost.py",
    "probe_save_flags.py",
    "probe_scene.py",
    "probe_sequence_accuracy.py",
    "probe_settings_persistence.py",
    "smoke_render.py",
    "motion_pipeline_cli.py",
    "render_sequences.py",
    "operators.py",
    "test_blender_integration.py",
    "test_render_workflow.py",   # reports fixture setup
    "panel_render_probe.py",     # prints a probe summary
}

DEBUG_PATTERNS = (
    (re.compile(r"\bbreakpoint\s*\("), "breakpoint() call"),
    (re.compile(r"^\s*print\(", re.MULTILINE), "top-level print()"),
    (re.compile(r"\bpdb\.set_trace\b"), "pdb.set_trace()"),
    (re.compile(r"\bTODO\b(?!:)"), "TODO marker"),
    (re.compile(r"\bFIXME\b"), "FIXME marker"),
    (re.compile(r"\bXXX\b"), "XXX marker"),
)


def imported_names(tree: ast.AST) -> "dict[str, int]":
    found: "dict[str, int]" = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found[alias.asname or alias.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    found[alias.asname or alias.name] = node.lineno
    return found


def used_names(tree: ast.AST) -> "set[str]":
    used: "set[str]" = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                used.add(base.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # String annotations such as "CharacterBox | None".
            for word in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", node.value):
                used.add(word)
    return used


def check_file(path: pathlib.Path) -> "list[str]":
    problems: "list[str]" = []
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]

    used = used_names(tree)
    exported = set(re.findall(r'"([A-Za-z_][A-Za-z_0-9]*)"', source))
    is_reexport = path.name in REEXPORT_HINTS
    for name, lineno in sorted(imported_names(tree).items(), key=lambda item: item[1]):
        if name in ALLOW or name in used or name in exported:
            continue
        if is_reexport:
            continue
        problems.append(f"line {lineno}: unused import {name!r}")

    for pattern, label in DEBUG_PATTERNS:
        if path.name in SKIP_DEBUG_SCAN:
            continue
        for match in pattern.finditer(source):
            lineno = source.count("\n", 0, match.start()) + 1
            problems.append(f"line {lineno}: {label}")
    return problems


def check_markdown(path: pathlib.Path) -> "list[str]":
    """Guard the docs against encoding damage.

    A Windows console round-trip through a non-UTF-8 code page replaces every
    character it cannot map with ``U+FFFD`` -- irreversibly.  That happened once
    to ``README.md`` (via ``Set-Content -Encoding UTF8``) and cost a full manual
    repair, so it is a hard failure here now.
    """
    problems: "list[str]" = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [f"not valid UTF-8: {exc}"]

    if "\ufffd" in text:
        problems.append(
            f"{text.count(chr(0xFFFD))} U+FFFD replacement character(s): the file was "
            "round-tripped through a lossy code page"
        )
    # These docs are English prose plus box art; East-Asian or private-use
    # characters here mean mojibake, never content.
    for index, line in enumerate(text.splitlines(), start=1):
        for char in line:
            code = ord(char)
            if (
                0x4E00 <= code <= 0x9FFF
                or 0xE000 <= code <= 0xF8FF
                or 0xFF00 <= code <= 0xFFEF
            ):
                problems.append(
                    f"line {index}: suspicious character U+{code:04X} {char!r} (mojibake?)"
                )
                break
    return problems


def main() -> int:
    files = sorted(p for p in ROOT.rglob("*.py") if "__pycache__" not in str(p))
    findings: "list[tuple[str, list[str]]]" = []
    for path in files:
        problems = check_file(path)
        if problems:
            findings.append((str(path.relative_to(ROOT)), problems))

    docs = sorted(ROOT.rglob("*.md"))
    for path in docs:
        problems = check_markdown(path)
        if problems:
            findings.append((str(path.relative_to(ROOT)), problems))

    print(f"checked {len(files)} python file(s) and {len(docs)} markdown file(s) under {ROOT}")
    if not findings:
        print("STATIC CHECK CLEAN")
        return 0
    for where, problems in findings:
        print(f"\n{where}")
        for problem in problems:
            print(f"  - {problem}")
    print(f"\n{sum(len(p) for _w, p in findings)} finding(s) in {len(findings)} file(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
