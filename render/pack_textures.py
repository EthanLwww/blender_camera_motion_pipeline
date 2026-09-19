"""Pack every external file a scene copy uses into the copy itself.

The scene copies a project ships keep the absolute asset paths of the machine
that generated them, which is the one remaining dependency when the project folder
is unpacked on a render node.  Either bridge those paths at render time
(``--path-map``) or remove the dependency once and for all by packing::

    blender -b -P pack_textures.py -- --scene-root "<project>/scene"

Packing rewrites each ``.blend`` in place (they get bigger; nothing else changes)
and writes ``pack_report.json`` next to the ``scene`` folder, listing what was
packed and what could not be found.  Missing files are reported, never fatal: a
scene that is missing a texture still renders, just not correctly.

This script is copied into every project folder by ``core/project.py``, so it
works from the project root as well as from ``<package>/render/``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# --------------------------------------------------------------------------
# make the add-on package importable whatever this file was copied next to
# --------------------------------------------------------------------------
def _find_bootstrap(start: str) -> str:
    """Nearest ``_bootstrap.py``: beside this script, one level up, or in a
    sibling/child package folder (which is how a project folder ships it)."""
    here = os.path.abspath(start)
    if not os.path.isdir(here):
        here = os.path.dirname(here)
    candidates = [os.path.join(here, "_bootstrap.py")]
    candidates.append(os.path.join(os.path.dirname(here), "_bootstrap.py"))
    try:
        for name in sorted(os.listdir(here)):
            candidates.append(os.path.join(here, name, "_bootstrap.py"))
    except OSError:
        pass
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _ensure_package_importable(start: str) -> str:
    import importlib.util

    found = _find_bootstrap(start)
    if found:
        spec = importlib.util.spec_from_file_location("_mpp_bootstrap", found)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        root = module.bootstrap(found)
        if root:
            return os.path.dirname(module.package_root(found))
    here = os.path.dirname(os.path.abspath(start))
    if here not in sys.path:
        sys.path.insert(0, here)
    return here


_PACKAGE_PARENT = _ensure_package_importable(__file__)

from blender_motion_pipeline.io.json_io import save_json_file  # noqa: E402
from blender_motion_pipeline.io.path_utils import (  # noqa: E402
    ensure_dir,
    normalize_path,
    to_forward_slashes,
)
from blender_motion_pipeline.utils.logging_utils import setup_logging  # noqa: E402

LOGGER = setup_logging()

REPORT_NAME = "pack_report.json"
BLEND_PATTERN = ".blend"


# --------------------------------------------------------------------------
# datablock inspection
# --------------------------------------------------------------------------
def external_files(blender_data=None) -> "list[dict]":
    """Every unpacked external reference in the loaded file."""
    import bpy

    data = blender_data if blender_data is not None else bpy.data
    found: "list[dict]" = []
    groups = (
        ("image", "images", "filepath"),
        ("library", "libraries", "filepath"),
        ("font", "fonts", "filepath"),
        ("sound", "sounds", "filepath"),
        ("movieclip", "movieclips", "filepath"),
        ("cache", "cache_files", "filepath"),
        ("volume", "volumes", "filepath"),
    )
    for kind, collection_name, attribute in groups:
        collection = getattr(data, collection_name, None)
        if collection is None:
            continue
        for datablock in collection:
            raw = getattr(datablock, attribute, "") or ""
            if getattr(datablock, "packed_file", None) is not None:
                continue
            if getattr(datablock, "source", "") == "GENERATED":
                continue
            if not str(raw).strip():
                continue
            found.append({"kind": kind, "name": str(datablock.name), "path": str(raw)})
    return found


def classify_external(files) -> "tuple[list[dict], list[dict]]":
    """Split *files* into those that exist on disk and those that do not."""
    import bpy

    present: "list[dict]" = []
    missing: "list[dict]" = []
    for entry in files:
        absolute = bpy.path.abspath(entry["path"]) or entry["path"]
        record = dict(entry)
        record["path"] = to_forward_slashes(absolute)
        if os.path.isfile(absolute):
            present.append(record)
        else:
            missing.append(record)
    return present, missing


def pack_loaded_file() -> "tuple[bool, str]":
    """Pack everything Blender currently has loaded.  Returns ``(ok, note)``."""
    import bpy

    try:
        result = bpy.ops.file.pack_all()
        state = set(result) if result else set()
        if "CANCELLED" in state:
            return False, "pack_all() reported CANCELLED"
        return True, "pack_all()"
    except Exception as exc:  # noqa: BLE001 - reported per scene
        LOGGER.warning("pack_all() failed (%s); packing images one by one", exc)
    packed = 0
    for image in getattr(bpy.data, "images", []):
        if image.packed_file is not None or image.source == "GENERATED":
            continue
        try:
            image.pack()
            packed += 1
        except Exception as exc:  # noqa: BLE001 - per datablock
            LOGGER.warning("could not pack image %s: %s", image.name, exc)
    if packed:
        return True, f"packed {packed} image(s) individually"
    return False, "nothing could be packed"


def pack_scene(path: str, *, compress: bool = True, dry_run: bool = False) -> dict:
    """Open one ``.blend``, pack it and save it back in place."""
    import bpy

    path = normalize_path(path)
    record = {
        "path": to_forward_slashes(path),
        "ok": False,
        "packed": [],
        "missing": [],
        "note": "",
        "error": "",
        "size_before": 0,
        "size_after": 0,
        "elapsed_seconds": 0.0,
    }
    started = time.time()
    try:
        record["size_before"] = os.path.getsize(path)
    except OSError:
        pass
    try:
        bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)
    except TypeError:
        bpy.ops.wm.open_mainfile(filepath=path)
    except Exception as exc:  # noqa: BLE001 - reported per scene
        record["error"] = f"cannot open {path}: {exc}"
        return record

    present, missing = classify_external(external_files())
    record["packed"] = present
    record["missing"] = missing
    if missing:
        LOGGER.warning("%s: %d external file(s) not found", os.path.basename(path), len(missing))

    if dry_run:
        record["ok"] = True
        record["note"] = f"dry run: {len(present)} file(s) would be packed"
        record["elapsed_seconds"] = round(time.time() - started, 3)
        return record

    if not present:
        record["ok"] = True
        record["note"] = "nothing external to pack"
        record["elapsed_seconds"] = round(time.time() - started, 3)
        return record

    ok, note = pack_loaded_file()
    record["note"] = note
    if not ok:
        record["error"] = note
        record["elapsed_seconds"] = round(time.time() - started, 3)
        return record
    try:
        bpy.ops.wm.save_as_mainfile(filepath=path, compress=bool(compress))
    except Exception as exc:  # noqa: BLE001 - reported per scene
        record["error"] = f"packed but could not save {path}: {exc}"
        record["elapsed_seconds"] = round(time.time() - started, 3)
        return record
    try:
        record["size_after"] = os.path.getsize(path)
    except OSError:
        pass
    record["ok"] = True
    record["elapsed_seconds"] = round(time.time() - started, 3)
    return record


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def collect_scenes(scene_root: str, *, recursive: bool = True) -> "list[str]":
    root = normalize_path(scene_root)
    if os.path.isfile(root):
        return [root]
    found: "list[str]" = []
    if recursive:
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name != "__pycache__"]
            found += [
                os.path.join(current, name)
                for name in sorted(filenames)
                if name.lower().endswith(BLEND_PATTERN)
            ]
    else:
        try:
            found = [
                os.path.join(root, name)
                for name in sorted(os.listdir(root))
                if name.lower().endswith(BLEND_PATTERN)
            ]
        except OSError:
            found = []
    return found


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pack_textures.py",
        description=(
            "Pack the external files a .blend depends on into the file itself, so a "
            "generated project folder renders without the original asset paths."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--scene-root", action="append", default=[], metavar="DIR",
                        help="folder holding scene copies (repeatable); default ./scene next to this script")
    parser.add_argument("--scene", action="append", default=[], metavar="FILE",
                        help="one .blend to pack (repeatable)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", default=True,
                        help="only look at the top level of --scene-root")
    parser.add_argument("--report", default="", help=f"where to write {REPORT_NAME}")
    parser.add_argument("--no-compress", dest="compress", action="store_false", default=True,
                        help="save uncompressed (larger files, faster saves)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be packed without writing anything")
    parser.add_argument("--list", action="store_true", help="only list the scenes that would be packed")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING or ERROR")
    return parser


def parse_args(argv=None):
    """Parse only the arguments after ``--``.

    Blender consumes its own flags before ``--``, so everything after it belongs to
    this script (``blender -b -P pack_textures.py -- --scene-root ./scene``); under
    a plain interpreter the whole ``argv`` is used.
    """
    if argv is None:
        argv = sys.argv
    if "--" in argv:
        args = argv[argv.index("--") + 1:]
    else:
        args = argv[1:]
    return build_parser().parse_args(args)


def main(argv=None) -> int:
    args = parse_args(argv)

    LOGGER.setLevel(getattr(logging, str(args.log_level).upper(), logging.INFO))

    here = os.path.dirname(os.path.abspath(__file__))
    roots = list(args.scene_root) or [os.path.join(here, "scene")]

    scenes: "list[str]" = [normalize_path(path) for path in args.scene]
    for root in roots:
        if not os.path.isdir(root) and not os.path.isfile(root):
            LOGGER.error("no such scene folder: %s", root)
            continue
        scenes += collect_scenes(root, recursive=bool(args.recursive))
    # de-duplicate, keep order
    seen = set()
    unique = []
    for path in scenes:
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)

    if not unique:
        LOGGER.error("no .blend file found to pack (looked in %s)", ", ".join(roots))
        return 1
    print(f"{len(unique)} scene(s) to pack")
    for path in unique:
        print(f"  {to_forward_slashes(path)}")
    if args.list:
        return 0

    report_path = args.report or os.path.join(
        os.path.dirname(normalize_path(roots[0])), REPORT_NAME
    )
    records = []
    failed = 0
    for path in unique:
        LOGGER.info("packing %s", os.path.basename(path))
        record = pack_scene(path, compress=bool(args.compress), dry_run=bool(args.dry_run))
        records.append(record)
        if record["ok"]:
            print(
                f"  OK   {os.path.basename(path)}: {record['note']}"
                f" (packed {len(record['packed'])}, missing {len(record['missing'])})"
            )
        else:
            failed += 1
            print(f"  FAIL {os.path.basename(path)}: {record['error'] or record['note']}")

    payload = {
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dry_run": bool(args.dry_run),
        "compress": bool(args.compress),
        "scene_count": len(unique),
        "failed": failed,
        "scenes": records,
    }
    ensure_dir(os.path.dirname(report_path))
    save_json_file(report_path, payload)
    print(f"report: {to_forward_slashes(report_path)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
