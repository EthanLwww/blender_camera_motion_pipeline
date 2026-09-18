"""Are the icon identifiers the UI uses valid in this Blender build?

    blender -b -P tests/probe_icons.py [-- "<package dir>"]

``UILayout.label(icon=...)`` takes an enum whose members change between Blender
versions and include very few "colour" names: ``SEQUENCE_COLOR_02`` exists as a
*strip colour* property value, not as an icon, and using it aborts the draw with a
wall-of-text ``TypeError`` in the panel -- exactly what happened in the render list.

This probe enumerates the icons this build actually accepts and checks every
``icon="..."`` literal in the package source against it.  The same check runs as a
case in ``test_addon_lifecycle`` so a bad icon cannot ship again.
"""
from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
for path in (_PACKAGE_PARENT, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import bpy  # noqa: E402

ICON_LITERAL = re.compile(r"""icon\s*=\s*["']([A-Za-z0-9_]+)["']""")


def valid_icons() -> "set[str]":
    """Every icon identifier this build's ``label(icon=...)`` accepts."""
    for owner in (bpy.types.UILayout, bpy.types.OperatorProperties):
        try:
            parameters = owner.bl_rna.functions["label"].parameters
            return {item.identifier for item in parameters["icon"].enum_items}
        except Exception:
            continue
    # Fallback: the same enum is exposed on the operator that draws props.
    try:
        return {item.identifier for item in bpy.types.UILayout.bl_rna.properties["icon"].enum_items}
    except Exception:
        return set()


def used_icons(package: str) -> "list[tuple[str, int, str]]":
    found = []
    for folder, dirs, files in os.walk(package):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8") as handle:
                for number, line in enumerate(handle, start=1):
                    for match in ICON_LITERAL.finditer(line):
                        found.append((os.path.relpath(path, package), number, match.group(1)))
    return found


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    package = os.path.abspath(argv[0]) if argv else os.path.dirname(_HERE)

    icons = valid_icons()
    print(f"package      : {package}")
    print(f"valid icons  : {len(icons)}")
    if not icons:
        print("could not enumerate the icon enum in this build")
        return 2

    used = used_icons(package)
    unknown = [(where, line, icon) for where, line, icon in used if icon not in icons]
    print(f"icon literals: {len(used)} in {len({u[0] for u in used})} file(s)")
    print(f"unique       : {sorted({u[2] for u in used})}")
    for where, line, icon in unknown:
        print(f"  UNKNOWN {where}:{line}  {icon}")
    print()
    if unknown:
        print(f"verdict: {len(unknown)} icon identifier(s) do not exist in this Blender build")
        # Suggest the closest real names, which is what a caller wants to see.
        for _where, _line, icon in unknown[:5]:
            head = icon.split("_")[0]
            near = sorted(name for name in icons if name.startswith(head))[:8]
            print(f"  {icon} -> did you mean: {near}")
        return 1
    print("verdict: OK - every icon literal exists in this build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
