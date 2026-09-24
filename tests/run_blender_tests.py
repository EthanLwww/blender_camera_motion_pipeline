"""Run every test suite that needs Blender::

    blender -b -P tests/run_blender_tests.py

Also runs the pure suites so a single command verifies the whole package, and
exits non-zero when anything fails (so a farm job can gate on it).
"""

from __future__ import annotations

import importlib
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
for path in (_PACKAGE_PARENT, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

# The suites import the add-on by name; make that name resolve to this folder
# whatever it is called (the package itself is name-agnostic -- see _bootstrap).
_BOOTSTRAP = os.path.join(os.path.dirname(_HERE), "_bootstrap.py")
if os.path.isfile(_BOOTSTRAP):
    import importlib.util as _ilu  # noqa: E402

    _spec = _ilu.spec_from_file_location("_mpp_bootstrap", _BOOTSTRAP)
    _bootstrap = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_bootstrap)
    _bootstrap.bootstrap(_BOOTSTRAP)

#: Suites in execution order.  ``pure`` ones need no bpy; ``blender`` ones do.
SUITES = (
    ("test_path_utils", "pure"),
    ("test_config", "pure"),
    ("test_project_layout", "pure"),
    ("test_motion_templates", "pure"),
    ("test_motion_composite", "pure"),
    ("test_region", "pure"),
    ("test_region_planner", "pure"),
    ("test_region_wiring", "pure"),
    ("test_camera_validation", "pure"),
    ("test_animation_api", "blender"),
    ("test_addon_lifecycle", "blender"),
    ("test_render_workflow", "blender"),
    ("test_focus_objects", "blender"),
    ("test_blender_integration", "blender"),
)


def run(*, only: str = "", verbose: bool = True) -> int:
    failures = 0
    summaries = []
    started = time.time()
    for name, kind in SUITES:
        if only and only not in name:
            continue
        if kind == "blender" and "bpy" not in sys.modules:
            print(f"[{name}] skipped: bpy is not available")
            continue
        print(f"\n{'=' * 72}\n{name} ({kind})\n{'=' * 72}")
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            print(f"[{name}] IMPORT FAILED: {type(exc).__name__}: {exc}")
            import traceback

            traceback.print_exc()
            failures += 1
            continue
        builder = getattr(module, "build_suite", None)
        if builder is None:
            print(f"[{name}] has no build_suite(); skipped")
            continue
        suite = builder()
        code = suite.run(verbose=verbose)
        failures += code
        summaries.append(suite.summary())

    print(f"\n{'=' * 72}")
    print(f"total: {sum(s['passed'] for s in summaries)} passed, "
          f"{sum(s['failed'] for s in summaries)} failed "
          f"in {time.time() - started:.1f}s")
    for summary in summaries:
        flag = "OK  " if summary["failed"] == 0 else "FAIL"
        print(f"  [{flag}] {summary['suite']}: {summary['passed']}/{summary['case_count']}")
        for case in summary["cases"]:
            if not case["ok"]:
                print(f"         - {case['name']}: {case['detail']}")
    print("=" * 72)
    print("ALL TESTS PASSED" if failures == 0 else f"{failures} SUITE FAILURE(S)")
    return 1 if failures else 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv
    only = ""
    if "--" in argv:
        extra = argv[argv.index("--") + 1:]
        if extra:
            only = extra[0]
    verbose = os.environ.get("MP_TEST_QUIET", "") == ""
    return run(only=only, verbose=verbose)


if __name__ == "__main__":
    raise SystemExit(main())
