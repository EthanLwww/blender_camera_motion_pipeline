"""Convert an Unreal-coordinate motion-template document to Blender coordinates.

The pipeline used to read Unreal-style templates and convert them at run time:
``location`` was centimetres along the camera's Unreal axes (``X`` forward,
``Y`` right, ``Z`` up) and ``rotation`` was ``[roll, pitch, yaw]`` degrees with
yaw about the **world** up axis.  Templates are Blender-native now and nothing is
converted, so a set written for Unreal has to be migrated once::

    python tests/migrate_unreal_templates.py --input templates.json --in-place
    python tests/migrate_unreal_templates.py --input templates.json --output blender.json
    python tests/migrate_unreal_templates.py --input templates.json --check

The conversion is exact for the numbers, and for the rotations it is the mapping
that reproduces the same shot for a level camera (which is what a render camera
is, and what every template in the reference set assumes):

===  ==========================================  ==========================================
     Unreal template                              Blender template
===  ==========================================  ==========================================
loc  ``[forward, right, up]`` cm                 ``[right, up, -forward]`` **m**
rot  ``[roll, pitch, yaw]`` (world-axis yaw)     ``[pitch, -yaw, -roll]`` (all camera-local)
===  ==========================================  ==========================================

Everything else -- ids, frames, focals, per-template and per-key extra fields,
the document's root shape, ``position``/``pos`` and ``angles``/``rot`` aliases,
keys given as a frame-keyed object -- is preserved.

The script is pure Python (no ``bpy``) and refuses to convert a document that
already looks Blender-native unless ``--force`` is given: Blender offsets are
metres, so a component above ``--threshold`` (default 20) is centimetres.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys

#: Keys that hold a location, in the order the parser accepts them.
LOCATION_KEYS = ("location", "position", "pos")
#: Keys that hold a rotation, in the order the parser accepts them.
ROTATION_KEYS = ("rotation", "angles", "rot")
#: Where the template list can live inside a document.
LIST_KEYS = ("templates", "motion_templates", "camera_templates")

#: A component above this in any location means the document is in centimetres.
DEFAULT_THRESHOLD = 20.0


def convert_location(values) -> "list[float]":
    """``[forward, right, up]`` cm -> ``[right, up, -forward]`` m."""
    forward, right, up = (float(values[i]) if i < len(values) else 0.0 for i in range(3))
    # ``or 0.0`` normalises -0.0, which is valid JSON but reads badly by hand.
    return [round(right / 100.0, 6) or 0.0, round(up / 100.0, 6) or 0.0,
            round(-forward / 100.0, 6) or 0.0]


def convert_rotation(values) -> "list[float]":
    """``[roll, pitch, yaw]`` -> camera-local ``[pitch, -yaw, -roll]``."""
    roll, pitch, yaw = (float(values[i]) if i < len(values) else 0.0 for i in range(3))
    return [round(pitch, 6) or 0.0, round(-yaw, 6) or 0.0, round(-roll, 6) or 0.0]


def _convert_keyframe(entry: dict) -> bool:
    """Convert one keyframe in place.  Returns True when anything changed."""
    changed = False
    for name in LOCATION_KEYS:
        if isinstance(entry.get(name), (list, tuple)) and len(entry[name]) >= 3:
            entry[name] = convert_location(entry[name])
            changed = True
            break
    for name in ROTATION_KEYS:
        if isinstance(entry.get(name), (list, tuple)) and len(entry[name]) >= 3:
            entry[name] = convert_rotation(entry[name])
            changed = True
            break
    return changed


def _convert_keys(keys) -> "tuple[int, int]":
    """Convert a template's keys (list, or dict keyed by frame)."""
    frames = 0
    changed = 0
    if isinstance(keys, dict):
        iterable = keys.values()
    elif isinstance(keys, list):
        iterable = keys
    else:
        return 0, 0
    for entry in iterable:
        if not isinstance(entry, dict):
            continue
        frames += 1
        if _convert_keyframe(entry):
            changed += 1
    return frames, changed


def convert_template(template: dict) -> "tuple[int, int]":
    return _convert_keys(template.get("keys"))


def find_templates(payload):
    """The template list inside *payload*, or ``None`` when there is none."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for name in LIST_KEYS:
            if isinstance(payload.get(name), list):
                return payload[name]
        # A ``{name: {...}}`` mapping is accepted by the parser too.
        values = [value for value in payload.values() if isinstance(value, dict)]
        if values and all("keys" in value for value in values):
            return values
    return None


def largest_location(payload) -> float:
    """The biggest absolute location component in the document (cm detector)."""
    biggest = 0.0
    templates = find_templates(payload) or []
    for template in templates:
        if not isinstance(template, dict):
            continue
        keys = template.get("keys")
        entries = keys.values() if isinstance(keys, dict) else (keys or [])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for name in LOCATION_KEYS:
                values = entry.get(name)
                if isinstance(values, (list, tuple)) and len(values) >= 3:
                    biggest = max(biggest, *(abs(float(v)) for v in values[:3]))
                    break
    return biggest


def convert_document(payload) -> "tuple[int, int]":
    """Convert every template in *payload* in place; returns (templates, keys)."""
    templates = find_templates(payload) or []
    converted_templates = 0
    converted_keys = 0
    for template in templates:
        if not isinstance(template, dict):
            continue
        frames, changed = convert_template(template)
        converted_keys += changed
        if frames:
            converted_templates += 1
    return converted_templates, converted_keys


_NUMBER_ARRAY = re.compile(r"\[\s*((?:-?[\d.]+(?:e-?\d+)?\s*,\s*)*-?[\d.]+)\s*\]")


def dumps(payload) -> str:
    """Pretty JSON with numeric arrays on one line (they are read by humans)."""
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    previous = None
    while previous != text:
        previous = text
        text = _NUMBER_ARRAY.sub(
            lambda match: "[" + ", ".join(part.strip() for part in match.group(1).split(",")) + "]",
            text,
        )
    return text + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_unreal_templates.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", required=True, help="template JSON to convert")
    parser.add_argument("--output", default="", help="write here (default: stdout)")
    parser.add_argument("--in-place", action="store_true",
                        help="overwrite --input (a .unreal_backup.json copy is kept)")
    parser.add_argument("--no-backup", action="store_true",
                        help="do not write the backup file with --in-place")
    parser.add_argument("--check", action="store_true",
                        help="only report what the document looks like")
    parser.add_argument("--force", action="store_true",
                        help="convert even when the document already looks Blender-native")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="a location component above this means centimetres")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    source = os.path.abspath(args.input)
    if not os.path.isfile(source):
        print(f"no such file: {source}")
        return 2
    with open(source, encoding="utf-8") as handle:
        try:
            payload = json.load(handle)
        except json.JSONDecodeError as exc:
            print(f"{source} is not valid JSON: {exc}")
            return 2

    templates = find_templates(payload)
    if templates is None:
        print(f"{source}: no template list found (expected an array, or a document with "
              f"one of {', '.join(LIST_KEYS)})")
        return 2
    biggest = largest_location(payload)
    print(f"{source}")
    print(f"  root       : {'array' if isinstance(payload, list) else 'object'}")
    print(f"  templates  : {len(templates)}")
    print(f"  largest loc: {biggest:g} (centimetres when above {args.threshold:g})")
    looks_blender = biggest <= args.threshold and biggest > 0.0
    if looks_blender:
        print("  verdict    : this already looks Blender-native "
              "(metres, so small numbers)")
    if args.check:
        return 0
    if looks_blender and not args.force:
        print("  refusing to convert; pass --force if it really is centimetres")
        return 1

    converted_templates, converted_keys = convert_document(payload)
    print(f"  converted  : {converted_templates} template(s), {converted_keys} keyframe(s)")
    if args.dry_run:
        print("  dry run    : nothing written")
        return 0

    text = dumps(payload)
    if args.in_place:
        if not args.no_backup:
            backup = os.path.join(
                os.path.dirname(source),
                os.path.splitext(os.path.basename(source))[0] + ".unreal_backup.json",
            )
            shutil.copy2(source, backup)
            print(f"  backup     : {backup}")
        target = source
    elif args.output:
        target = os.path.abspath(args.output)
    else:
        sys.stdout.write(text)
        return 0

    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print(f"  wrote      : {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
