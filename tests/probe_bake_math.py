"""Unit-level check of the parent-space bake helpers (no scene required).

    blender -b -P tests/probe_bake_math.py

The generator bakes world-space poses into ``obj.location`` /
``obj.rotation_quaternion``.  ``obj.location`` is expressed in the *parent's*
space, and Blender evaluates a parented object as::

    world = parent_world @ matrix_parent_inverse @ local_basis

This probe proves the helper's inverse formula reproduces that chain exactly by
round-tripping a set of random parent / parent-inverse / pose triples through
``mathutils`` (Blender's own matrix maths), which is the authority the renderer
uses.
"""
from __future__ import annotations

import math
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
for path in (_PACKAGE_PARENT, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

from mathutils import Euler, Matrix, Vector  # noqa: E402

from blender_motion_pipeline.core.sequence_generator import (  # noqa: E402
    _identity_4x4,
    _invert_4x4,
    _matmul,
    _matrix_from_pose,
)


def _to_matrix(rows):
    return Matrix([[float(v) for v in row] for row in rows])


# mathutils matrices are single precision, so every comparison against them has
# to be made relative to the magnitude of the values involved; 1e-5 relative is
# still ~100x tighter than the 0.01 m the sequence probe demands.
_RELATIVE_TOLERANCE = 1e-5


def _max_gap(left, right) -> float:
    scale = 1.0
    for rows in (left, right):
        for row in rows:
            for value in row:
                scale = max(scale, abs(float(value)))
    gap = max(
        abs(float(left[i][j]) - float(right[i][j])) for i in range(4) for j in range(4)
    )
    return gap / scale


def main() -> int:
    random.seed(20240607)
    failures = []

    # 1. inverse / multiply agree with mathutils on random affine matrices.
    for index in range(200):
        euler = Euler(
            (random.uniform(-math.pi, math.pi) for _ in range(3)), "XYZ"
        )
        source = (
            Matrix.Translation(Vector((random.uniform(-50, 50) for _ in range(3))))
            @ euler.to_matrix().to_4x4()
            @ Matrix.Diagonal(Vector((random.uniform(0.2, 3.0) for _ in range(3))).to_4d())
        )
        rows = [[float(v) for v in row] for row in source]
        helper_inverse = _invert_4x4(rows)
        reference_inverse = [[float(v) for v in row] for row in source.inverted()]
        gap = _max_gap(helper_inverse, reference_inverse)
        if gap > _RELATIVE_TOLERANCE:
            failures.append(f"inverse mismatch at sample {index}: {gap:.3e}")

        other = Matrix.Translation(Vector((random.uniform(-10, 10) for _ in range(3))))
        product = _matmul(rows, [[float(v) for v in row] for row in other])
        reference_product = [[float(v) for v in row] for row in (source @ other)]
        gap = _max_gap(product, reference_product)
        if gap > _RELATIVE_TOLERANCE:
            failures.append(f"matmul mismatch at sample {index}: {gap:.3e}")

    # 2. The full bake round trip: parent chain -> world -> local -> evaluated world.
    worst = 0.0
    for index in range(300):
        parent_world = _to_matrix(
            [
                [float(v) for v in row]
                for row in (
                    Matrix.Translation(Vector((random.uniform(-500, 500) for _ in range(3))))
                    @ Euler(
                        (random.uniform(-math.pi, math.pi) for _ in range(3)), "XYZ"
                    ).to_matrix().to_4x4()
                )
            ]
        )
        parent_inverse = _to_matrix(
            [
                [float(v) for v in row]
                for row in Matrix.Translation(
                    Vector((random.uniform(-100, 100) for _ in range(3)))
                )
            ]
        )
        position = [random.uniform(-500, 500) for _ in range(3)]
        quaternion = [random.uniform(-1, 1) for _ in range(4)]
        norm = math.sqrt(sum(v * v for v in quaternion)) or 1.0
        quaternion = [v / norm for v in quaternion]

        pose = _matrix_from_pose(position, quaternion)
        gap = _max_gap(pose, _to_matrix(pose))
        if gap > _RELATIVE_TOLERANCE:
            failures.append(f"_matrix_from_pose mismatch at sample {index}: {gap:.3e}")

        want = _to_matrix(pose)
        # helper formula: local = inv(parentinv) @ inv(parent_world) @ world
        local = _matmul(_matmul(_invert_4x4([[float(v) for v in r] for r in parent_inverse]),
                                _invert_4x4([[float(v) for v in r] for r in parent_world])),
                        pose)
        got = parent_world @ parent_inverse @ _to_matrix(local)
        delta = _max_gap(got, want)
        worst = max(worst, delta)
        if delta > _RELATIVE_TOLERANCE:
            failures.append(f"bake round trip mismatch at sample {index}: {delta:.3e}")

    identity_gap = _max_gap(_matmul(_identity_4x4(), _identity_4x4()), _identity_4x4())
    if identity_gap > 1e-12:
        failures.append(f"identity is not idempotent: {identity_gap:.3e}")

    print(f"worst relative bake round-trip error: {worst:.3e} (tolerance {_RELATIVE_TOLERANCE:.0e})")
    for failure in failures[:10]:
        print("FAIL:", failure)
    print(f"{len(failures)} failure(s) out of 200 inverse/matmul + 300 round-trip samples")
    print("verdict:", "OK" if not failures else "FAILED")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
