"""Compound shots: play several base templates one after another in one sequence.

A compound ("复合运镜") keeps the **same total frame range** as a single template --
0..80 frames for the reference set, or whatever ``motion.frame_start/frame_end``
says -- and only *concatenates the order*: the range is split into one window per
part, each part is played inside its own window, and each part is anchored on the
pose the previous one ended in.  So ``pan_right + hitchcock`` pans right over the
first half and pushes in over the second, continuously, in 81 frames.

How it works: a recipe is **flattened into one ordinary template** whose keys are
the chained poses expressed in the *anchor* frame (the frame the first part starts
in).  Everything downstream -- validation, the camera search, the bake, the
metadata, the renderer -- then treats a compound exactly like any other template,
which is why this module needs no hooks anywhere else.

The maths, for parts ``P1 .. Pk`` with accumulated offset ``p`` (a vector in the
anchor frame) and accumulated rotation ``q`` (a quaternion in the anchor frame)::

    pose(t) in Pi  =  p + R(q) * o_i(t)        (position)
                      q * r_i(t)               (orientation)

and at the end of every part ``p += R(q) * o_i(end)``, ``q *= r_i(end)``.  ``o`` and
``r`` are the part's own template values, read through the very same
``MotionTemplateGenerator.local_offset`` / ``rotation_delta`` the generator uses, so
a compound cannot drift from what the parts do on their own.

Keys are emitted for **every frame** of the range, which makes the flattened result
independent of the interpolation mode: sampling it reproduces the chained parts
exactly instead of re-interpolating a retimed curve.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from ..config.models import ConfigError, TemplateUnitScale
from .motion_templates import (
    MotionTemplate,
    MotionTemplateGenerator,
    MotionTemplateLibrary,
    TemplateKeyframe,
    quat_multiply,
    quat_normalize,
    quat_rotate,
    quat_to_euler_xyz,
)

#: Compound output modes (``CompositeSection.output_mode``).
OUTPUT_WITH_BASE = "with_base"          # generate base shots and compounds
OUTPUT_ONLY_COMPOUND = "only_compound"   # compounds only
OUTPUT_ONLY_BASE = "only_base"           # base shots only (compounds are disabled)
OUTPUT_MODES = (OUTPUT_WITH_BASE, OUTPUT_ONLY_COMPOUND, OUTPUT_ONLY_BASE)

#: Compound construction modes (``CompositeSection.mode``).
MODE_FULL = "full"        # every ordering of every template: n!
MODE_PARTIAL = "partial"  # x-part orderings, a chosen number of them
MODE_NAMES = (MODE_FULL, MODE_PARTIAL)

#: Longest motion-folder name a compound may produce; longer ones get a hash suffix.
MAX_NAME_LENGTH = 110


def factorial(n: int) -> int:
    return math.factorial(int(n))


def ordered_count(n: int, x: int) -> int:
    """Ordered selections of ``x`` distinct templates out of ``n``: ``x! * C(n, x)``."""
    n = int(n)
    x = int(x)
    if x < 0 or n < 0 or x > n:
        return 0
    return math.perm(n, x)


# --------------------------------------------------------------------------
# recipes
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CompoundRecipe:
    """One ordered combination of base templates."""

    parts: "tuple[str, ...]"
    index: int = 0

    @property
    def full_name(self) -> str:
        """``compound_<part>+<part>...`` -- the motion folder this recipe generates."""
        return "compound_" + "+".join(self.parts)

    @property
    def name(self) -> str:
        """``full_name``, shortened with a hash when it would be unwieldy."""
        full = self.full_name
        if len(full) <= MAX_NAME_LENGTH:
            return full
        digest = f"{abs(hash(full)) % (16 ** 6):06x}"
        keep = MAX_NAME_LENGTH - len(digest) - 1
        return f"{full[:keep]}_{digest}"

    def to_dict(self) -> dict:
        return {"parts": list(self.parts), "index": int(self.index), "name": self.name}


def _sample_recipes(names: "list[str]", x: int, count: int, seed: int) -> "list[tuple[str, ...]]":
    """``count`` distinct ordered ``x``-tuples of ``names``, deterministically."""
    space = ordered_count(len(names), x)
    count = max(0, min(int(count), space))
    rng = random.Random(int(seed))
    if count <= 0:
        return []
    if space <= 200_000:
        # Small enough to enumerate: sampling from the full list is unbiased and
        # avoids the birthday problem of rejection sampling.
        pool = [tuple(combo)
                for combo in _all_ordered(names, x)]
        return rng.sample(pool, count)
    chosen: "list[tuple[str, ...]]" = []
    seen: "set[tuple[str, ...]]" = set()
    guard = 0
    while len(chosen) < count and guard < count * 50 + 1000:
        guard += 1
        combo = tuple(rng.sample(names, x))
        if combo in seen:
            continue
        seen.add(combo)
        chosen.append(combo)
    return chosen


def _all_ordered(names: "list[str]", x: int):
    """Every ordered ``x``-tuple of ``names`` (generator)."""
    import itertools

    return itertools.permutations(names, x)


def build_recipes(
    names: "list[str]",
    *,
    mode: str = MODE_FULL,
    types_per_sequence: int = 2,
    sequence_count: int = 12,
    seed: int = 1234,
    max_full_sequences: int = 5040,
    max_partial_sequences: int = 100000,
) -> "tuple[list[CompoundRecipe], list[str]]":
    """Build the compound recipes a run should generate.

    Returns ``(recipes, warnings)``; raises :class:`ConfigError` when the request
    cannot be honoured at all (an unknown mode, or a full compound whose ``n!``
    exceeds ``max_full_sequences``).
    """
    names = [str(name) for name in names if str(name)]
    warnings: "list[str]" = []
    if mode not in MODE_NAMES:
        raise ConfigError(f"composite mode must be one of {MODE_NAMES}, got {mode!r}")
    if not names:
        return [], ["no templates are loaded, so no compound shot can be built"]

    if mode == MODE_FULL:
        wanted = factorial(len(names))
        if wanted > int(max_full_sequences):
            raise ConfigError(
                f"a full compound of {len(names)} templates is {wanted} sequences "
                f"(n!), above the {int(max_full_sequences)} limit -- narrow the template "
                "set with the motion filter, or switch to a partial compound"
            )
        recipes = [CompoundRecipe(parts=combo, index=i + 1)
                   for i, combo in enumerate(_all_ordered(names, len(names)))]
        if len(names) == 1:
            warnings.append(
                "a full compound needs at least two templates; only one is loaded"
            )
        return recipes, warnings

    x = int(types_per_sequence)
    if x < 2:
        raise ConfigError("a partial compound needs at least 2 templates per sequence")
    if x > len(names):
        raise ConfigError(
            f"a partial compound of {x} templates per sequence needs at least {x} "
            f"templates, but only {len(names)} are loaded"
        )
    space = ordered_count(len(names), x)
    count = int(sequence_count)
    if count > int(max_partial_sequences):
        warnings.append(
            f"partial compound count {count} is capped at {int(max_partial_sequences)}"
        )
        count = int(max_partial_sequences)
    if count > space:
        warnings.append(
            f"only {space} distinct {x}-part ordering(s) exist; generating {space} "
            f"instead of {count}"
        )
        count = space
    sampled = _sample_recipes(names, x, count, seed)
    recipes = [CompoundRecipe(parts=parts, index=i + 1) for i, parts in enumerate(sampled)]
    return recipes, warnings


# --------------------------------------------------------------------------
# frame windows
# --------------------------------------------------------------------------
def split_windows(frame_start: int, frame_end: int, parts: int) -> "list[tuple[int, int]]":
    """Split ``[frame_start, frame_end]`` into ``parts`` contiguous windows.

    The total is preserved exactly (``sum(length) == frame_end - frame_start + 1``)
    and no frame is shared between two windows, so a compound has the same frame
    count as a single template.
    """
    parts = max(1, int(parts))
    start = int(frame_start)
    end = int(frame_end)
    total = max(parts, end - start + 1)
    base, remainder = divmod(total, parts)
    windows: "list[tuple[int, int]]" = []
    cursor = start
    for index in range(parts):
        length = base + (1 if index < remainder else 0)
        windows.append((cursor, cursor + length - 1))
        cursor += length
    return windows


# --------------------------------------------------------------------------
# flattening
# --------------------------------------------------------------------------
def compound_range(library: MotionTemplateLibrary, parts, *, frame_start=None,
                   frame_end=None) -> "tuple[int, int]":
    """The frame range a compound of ``parts`` occupies.

    ``frame_start``/``frame_end`` (the motion section) win when set; otherwise the
    parts' own span is used, so a compound is exactly as long as the templates it
    is made of.
    """
    templates = [library.get(name) for name in parts]
    start = int(frame_start) if frame_start is not None else min(t.frame_min for t in templates)
    end = int(frame_end) if frame_end is not None else max(t.frame_max for t in templates)
    if end < start:
        raise ConfigError(f"compound frame range is inverted: {start}..{end}")
    return start, end


def flatten_recipe(
    recipe: CompoundRecipe,
    library: MotionTemplateLibrary,
    *,
    frame_start: int | None = None,
    frame_end: int | None = None,
    interpolation: str = "BEZIER",
    rotation_order: str = "XYZ",
    logger=None,
) -> MotionTemplate:
    """Turn a recipe into one ordinary template spanning the same frame range.

    See the module docstring for the maths.  The result's keys are the chained pose
    at **every** frame, expressed in the anchor frame, so the generator reproduces
    the concatenation exactly however interpolation is configured.
    """
    if rotation_order != "XYZ":
        # The flattened keys are Euler XYZ (``quat_to_euler_xyz``); any other order
        # would be read back with different axes.
        if logger is not None:
            logger.warning(
                "compound shots are flattened as Euler XYZ; motion.unit_scale."
                "rotation_order=%r is ignored for them", rotation_order,
            )
    unit_scale = TemplateUnitScale(fps=24.0, rotation_order="XYZ")
    generator = MotionTemplateGenerator(
        unit_scale=unit_scale, frame_start=0, frame_scale=1.0, interpolation=interpolation
    )
    templates = [library.get(name) for name in recipe.parts]
    start, end = compound_range(library, recipe.parts,
                               frame_start=frame_start, frame_end=frame_end)
    windows = split_windows(start, end, len(templates))

    declares_focal = any(template.focals() for template in templates)
    keys: "list[TemplateKeyframe]" = []
    offset = (0.0, 0.0, 0.0)                  # accumulated position, anchor frame
    orientation = (1.0, 0.0, 0.0, 0.0)        # accumulated rotation, anchor frame
    carried_focal: "float | None" = None
    part_windows: "list[list[int]]" = []

    for template, (window_start, window_end) in zip(templates, windows):
        part_windows.append([window_start, window_end])
        template_start = float(template.frame_min)
        template_end = float(template.frame_max)
        span = max(1.0, template_end - template_start)
        window_span = max(1.0, float(window_end - window_start))
        part_end = template.interpolate(template_end)
        for frame in range(window_start, window_end + 1):
            time = template_start + (frame - window_start) * span / window_span
            key = template.interpolate(time)
            local = generator.local_offset(key.location)
            rotated = quat_rotate(orientation, local)
            position = tuple(offset[i] + rotated[i] for i in range(3))
            total = quat_normalize(quat_multiply(orientation, generator.rotation_delta(key.rotation)))
            euler = [math.degrees(value) for value in quat_to_euler_xyz(total)]
            focal: "float | None" = None
            if declares_focal:
                if template.focals():
                    focal = float(key.focal)
                    carried_focal = focal
                else:
                    focal = carried_focal
            keys.append(TemplateKeyframe(
                frame=frame,
                location=tuple(float(v) for v in position),
                rotation=(float(euler[0]), float(euler[1]), float(euler[2])),
                focal=focal,
            ))
        # Advance the running pose to this part's end pose.
        local_end = generator.local_offset(part_end.location)
        rotated_end = quat_rotate(orientation, local_end)
        offset = tuple(offset[i] + rotated_end[i] for i in range(3))
        orientation = quat_normalize(
            quat_multiply(orientation, generator.rotation_delta(part_end.rotation))
        )
        if declares_focal and template.focals():
            carried_focal = float(part_end.focal)

    template = MotionTemplate(
        name=recipe.name,
        keyframes=keys,
        description="compound: " + " -> ".join(recipe.parts),
        source=library.source,
        parameters={
            "compound": {
                "parts": list(recipe.parts),
                "windows": part_windows,
                "range": [start, end],
                "index": int(recipe.index),
                "mode": "compound",
            }
        },
    )
    template.validate()
    return template


def build_compound_templates(
    library: MotionTemplateLibrary,
    *,
    mode: str = MODE_FULL,
    types_per_sequence: int = 2,
    sequence_count: int = 12,
    seed: int = 1234,
    max_full_sequences: int = 5040,
    max_partial_sequences: int = 100000,
    frame_start: int | None = None,
    frame_end: int | None = None,
    interpolation: str = "BEZIER",
    rotation_order: str = "XYZ",
    logger=None,
) -> "tuple[list[MotionTemplate], list[str]]":
    """Recipes for *library*, flattened into templates ready to generate."""
    recipes, warnings = build_recipes(
        [template.name for template in library],
        mode=mode,
        types_per_sequence=types_per_sequence,
        sequence_count=sequence_count,
        seed=seed,
        max_full_sequences=max_full_sequences,
        max_partial_sequences=max_partial_sequences,
    )
    templates: "list[MotionTemplate]" = []
    for recipe in recipes:
        templates.append(flatten_recipe(
            recipe, library,
            frame_start=frame_start, frame_end=frame_end,
            interpolation=interpolation, rotation_order=rotation_order, logger=logger,
        ))
    return templates, warnings


def describe_counts(total_templates: int, *, types_per_sequence: int = 2,
                    mode: str = MODE_FULL) -> str:
    """One line for the panel: how many compounds this configuration means."""
    if mode == MODE_FULL:
        return f"full compound: {factorial(total_templates)} sequence(s)  ({total_templates}!)"
    x = int(types_per_sequence)
    space = ordered_count(total_templates, x)
    return (
        f"partial compound: up to {space} distinct {x}-part ordering(s)  "
        f"({x}! x C({total_templates},{x}))"
    )
