"""Version-agnostic access to an Action's F-Curves.

Blender 5.0 replaced the flat ``action.fcurves`` container with *layered*
actions::

    Action
      layers[]            (``action.layers``)
        strips[]          (keyframe strips)
          channelbags[]   (one per slot)
            fcurves[]

The old path is gone in 5.2 (``action.fcurves`` no longer exists at all), while
older releases have no ``layers``.  Everything in this pipeline that needs to
inspect, retime or count curves goes through :func:`action_fcurves` so the same
code works on both.
"""

from __future__ import annotations

from typing import Iterable


def action_fcurves(action) -> "list":
    """Every F-Curve in ``action``, across all layers/strips/channelbags.

    Returns an empty list for ``None`` or for an action with no curves, which
    keeps callers free of ``None`` checks.
    """
    if action is None:
        return []
    flat = getattr(action, "fcurves", None)
    if flat is not None:
        try:
            return list(flat)
        except Exception:
            pass

    curves: "list" = []
    layers = getattr(action, "layers", None)
    if not layers:
        return curves
    for layer in layers:
        for strip in getattr(layer, "strips", []) or []:
            bags = getattr(strip, "channelbags", None)
            if bags:
                for bag in bags:
                    curves.extend(list(getattr(bag, "fcurves", []) or []))
                continue
            # Some builds expose a single channelbag per strip.
            single = getattr(strip, "channelbag", None)
            if single is not None:
                curves.extend(list(getattr(single, "fcurves", []) or []))
                continue
            curves.extend(list(getattr(strip, "fcurves", []) or []))
    return curves


def count_fcurves(action) -> int:
    return len(action_fcurves(action))


def action_data_paths(action) -> "list[str]":
    """Sorted, de-duplicated data paths animated by ``action``."""
    return sorted({curve.data_path for curve in action_fcurves(action)})


def iter_keyframes(action) -> "Iterable":
    """Yield ``(curve, keyframe_point)`` for every key in ``action``."""
    for curve in action_fcurves(action):
        for keyframe in getattr(curve, "keyframe_points", []) or []:
            yield curve, keyframe


def set_interpolation(action, interpolation: str) -> int:
    """Set the interpolation of every key in ``action``; returns keys touched."""
    touched = 0
    for curve, keyframe in iter_keyframes(action):
        keyframe.interpolation = interpolation
        touched += 1
    for curve in action_fcurves(action):
        try:
            curve.update()
        except Exception:
            continue
    return touched


def action_names_for(owner) -> "list[str]":
    """Every action name reachable from ``owner`` (its action and NLA strips)."""
    names: "list[str]" = []
    animation_data = getattr(owner, "animation_data", None)
    if animation_data is None:
        return names
    action = getattr(animation_data, "action", None)
    if action is not None and action.name not in names:
        names.append(action.name)
    for track in getattr(animation_data, "nla_tracks", []) or []:
        for strip in getattr(track, "strips", []) or []:
            strip_action = getattr(strip, "action", None)
            if strip_action is not None and strip_action.name not in names:
                names.append(strip_action.name)
    return names


def actions_for(owner) -> "list":
    """Action datablocks referenced by ``owner`` (direct action + NLA strips)."""
    actions = []
    animation_data = getattr(owner, "animation_data", None)
    if animation_data is None:
        return actions
    action = getattr(animation_data, "action", None)
    if action is not None:
        actions.append(action)
    for track in getattr(animation_data, "nla_tracks", []) or []:
        for strip in getattr(track, "strips", []) or []:
            strip_action = getattr(strip, "action", None)
            if strip_action is not None and strip_action not in actions:
                actions.append(strip_action)
    return actions


def assign_action(owner, action) -> bool:
    """Bind ``action`` to ``owner``, coping with Blender 4.4+ action slots.

    Without a slot the action has no effect even though assignment "succeeded",
    so this is not optional on 5.x.
    """
    try:
        if getattr(owner, "animation_data", None) is None:
            owner.animation_data_create()
        animation_data = owner.animation_data
        if animation_data is None:
            return False
        animation_data.action = action
        if hasattr(animation_data, "action_slot") and animation_data.action_slot is None:
            slots = getattr(action, "slots", None)
            if slots:
                try:
                    animation_data.action_slot = slots[0]
                except Exception:
                    pass
        return True
    except Exception:
        return False


def clear_animation(owner) -> int:
    """Detach actions/NLA from ``owner``; returns how many actions were dropped."""
    removed = 0
    animation_data = getattr(owner, "animation_data", None)
    if animation_data is None:
        return removed
    if getattr(animation_data, "action", None) is not None:
        removed += 1
    animation_data.action = None
    for track in list(getattr(animation_data, "nla_tracks", []) or []):
        animation_data.nla_tracks.remove(track)
        removed += 1
    return removed
