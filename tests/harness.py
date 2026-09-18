"""Minimal dependency-free test harness.

Deliberately does not use :mod:`unittest` discovery so the same files can be
executed from a plain interpreter, from Blender's Python, and from the
``--run-tests`` flag of the pipeline CLI.
"""

from __future__ import annotations

import os
import sys
import traceback


class Failure(AssertionError):
    pass


class Case:
    """One named check."""

    def __init__(self, name: str, fn, *, expect_raises=None):
        self.name = name
        self.fn = fn
        self.expect_raises = expect_raises


class Suite:
    """Collects cases and runs them, returning a process exit code."""

    def __init__(self, name: str):
        self.name = name
        self.cases: "list[Case]" = []
        self.setup = None
        self.teardown = None
        self.results: "list[tuple[str, bool, str]]" = []

    def case(self, name: str, *, expect_raises=None):
        def decorator(fn):
            self.cases.append(Case(name, fn, expect_raises=expect_raises))
            return fn
        return decorator

    def requires_blender(self) -> bool:
        return True

    def run(self, *, verbose: bool = True, stop_on_failure: bool = False) -> int:
        failures = 0
        if self.setup is not None:
            try:
                self.setup()
            except Exception:
                print(f"[{self.name}] SETUP FAILED")
                traceback.print_exc()
                return 1
        for case in self.cases:
            try:
                case.fn()
                if case.expect_raises is not None:
                    self.results.append((case.name, False, f"expected {case.expect_raises.__name__} but nothing was raised"))
                    failures += 1
                    if verbose:
                        print(f"  FAIL {case.name}: expected an exception")
                    continue
                self.results.append((case.name, True, ""))
                if verbose:
                    print(f"  ok   {case.name}")
            except Exception as exc:
                if case.expect_raises is not None and isinstance(exc, case.expect_raises):
                    self.results.append((case.name, True, ""))
                    if verbose:
                        print(f"  ok   {case.name} (raised {type(exc).__name__} as expected)")
                    continue
                failures += 1
                detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                self.results.append((case.name, False, detail))
                if verbose:
                    print(f"  FAIL {case.name}: {detail}")
                    if os.environ.get("MP_TEST_TRACEBACK"):
                        traceback.print_exc()
                if stop_on_failure:
                    break
        if self.teardown is not None:
            try:
                self.teardown()
            except Exception:
                print(f"[{self.name}] TEARDOWN FAILED")
                traceback.print_exc()
                failures += 1
        total = len(self.results)
        passed = sum(1 for _n, ok, _d in self.results if ok)
        status = "PASS" if failures == 0 else "FAIL"
        print(f"[{self.name}] {status}: {passed}/{total} case(s)")
        return 0 if failures == 0 else 1

    def summary(self) -> dict:
        return {
            "suite": self.name,
            "case_count": len(self.results),
            "passed": sum(1 for _n, ok, _d in self.results if ok),
            "failed": sum(1 for _n, ok, _d in self.results if not ok),
            "cases": [{"name": n, "ok": ok, "detail": d} for n, ok, d in self.results],
        }


# --------------------------------------------------------------------------
# assertions
# --------------------------------------------------------------------------
def ok(condition, message: str = "expected a truthy value") -> None:
    if not condition:
        raise Failure(message)


def equal(actual, expected, message: str = "") -> None:
    if actual != expected:
        raise Failure(f"{message or 'values differ'}: expected {expected!r}, got {actual!r}")


def close(actual, expected, tol: float = 1e-6, message: str = "") -> None:
    if actual is None or expected is None:
        raise Failure(f"{message or 'comparison'}: got {actual!r} / {expected!r}")
    if abs(float(actual) - float(expected)) > tol:
        raise Failure(f"{message or 'values differ'}: expected {expected} +/- {tol}, got {actual}")


def vec_close(actual, expected, tol: float = 1e-6, message: str = "") -> None:
    if len(actual) != len(expected):
        raise Failure(f"{message or 'vectors differ'}: length {len(actual)} vs {len(expected)}")
    for index, (a, b) in enumerate(zip(actual, expected)):
        if abs(float(a) - float(b)) > tol:
            raise Failure(
                f"{message or 'vectors differ'} at index {index}: "
                f"expected {tuple(round(float(v), 6) for v in expected)}, "
                f"got {tuple(round(float(v), 6) for v in actual)}"
            )


def raises(exc_type, fn, message: str = "") -> Exception:
    try:
        fn()
    except exc_type as exc:
        return exc
    except Exception as exc:  # wrong type
        raise Failure(
            f"{message or 'expected an exception'}: wanted {exc_type.__name__}, "
            f"got {type(exc).__name__}: {exc}"
        ) from exc
    raise Failure(f"{message or 'expected an exception'}: {exc_type.__name__} was not raised")


def approx_ratio(actual, expected, tol: float = 0.05, message: str = "") -> None:
    if expected == 0:
        if abs(actual) > tol:
            raise Failure(f"{message or 'ratio'}: expected ~0, got {actual}")
        return
    if abs(float(actual) - float(expected)) / abs(float(expected)) > tol:
        raise Failure(f"{message or 'ratio'}: expected ~{expected}, got {actual}")


# --------------------------------------------------------------------------
# discovery / execution
# --------------------------------------------------------------------------
def discover(package_dir: str) -> "list[str]":
    names = []
    for entry in sorted(os.listdir(package_dir)):
        if entry.startswith("test_") and entry.endswith(".py"):
            names.append(entry[:-3])
    return names


def run_suites(suites, *, verbose: bool = True) -> int:
    failures = 0
    summaries = []
    for suite in suites:
        failures += suite.run(verbose=verbose)
        summaries.append(suite.summary())
    return failures


def main(argv=None) -> int:
    """Run every pure suite in this package (no Blender required)."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import importlib

    pure = ("test_path_utils", "test_config", "test_motion_templates", "test_camera_validation")
    failures = 0
    for name in pure:
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            print(f"[{name}] IMPORT FAILED: {exc}")
            failures += 1
            continue
        builder = getattr(module, "build_suite", None)
        if builder is None:
            print(f"[{name}] no build_suite(); skipped")
            continue
        failures += builder().run()
    print("ALL PURE TESTS PASSED" if failures == 0 else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
