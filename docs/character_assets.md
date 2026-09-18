# Character assets

This pipeline's character support is a **pluggable adapter**. It ships complete
and functional for Blender-native character libraries, and it degrades honestly
when no library is configured.

> **Status in this workspace: no character asset is available.**
> The adapter is implemented and unit-tested against test doubles, and the
> `null` / `unreal_metahuman` providers are exercised for real. No MetaHuman or
> Blender rig exists here, so no character sequence has been produced from a real
> asset. The character-free workflow is fully functional.

---

## Why MetaHuman assets are not used directly

The reference project is an Unreal plugin whose characters are MetaHuman
blueprints backed by a UE-specific skeleton, UE animation sequences and UE
material graphs. A `.blend` cannot load any of those:

* the skeleton and skin weights are UE assets, not interchange files;
* the animation clips are `UAnimSequence`, not FBX/glTF;
* the groom/hair and material setup has no Blender equivalent.

Bridging them requires a separate retarget-and-export step (for example
MetaHuman → FBX via Unreal's exporter, then an FBX → Blender rig import), which is
outside this pipeline. `UnrealMetaHumanProvider` exists to make that explicit in
the logs rather than looking like a misconfiguration.

## The interface

```python
class CharacterProvider(abc.ABC):
    name: str
    description: str

    def status(self) -> str: ...                    # available | degraded | unavailable | not_implemented
    def availability(self) -> dict: ...             # structured, for logs and JSON
    def is_usable(self) -> bool: ...

    def list_characters(self) -> list[CharacterDescriptor]: ...
    def list_animations(self) -> list[AnimationDescriptor]: ...

    def import_character(self, scene_context, character_config) -> CharacterPlacement: ...
    def place_character(self, placement, scene_context) -> CharacterPlacement: ...
    def apply_animation(self, placement, animation_config) -> CharacterPlacement: ...
    def validate_character_placement(self, placement, scene_context) -> CharacterValidation: ...
```

Contract: **a missing asset never raises.** Every method returns a structured
result whose `status`/`ok` says what actually happened. `CharacterPlacement.ok`
is only ever `True` when an object really exists in the scene — the providers are
forbidden from reporting optimistic success, because the sequence metadata records
`has_character` from it.

| Provider | `status()` | Notes |
|---|---|---|
| `blender` | `available` / `degraded` / `unavailable` | Real adapter (below). |
| `null` | `unavailable` | Full interface, no assets, with an actionable reason. Default when nothing is configured. |
| `unreal_metahuman` | `not_implemented` | Documents the platform boundary. |
| `auto` | resolves at runtime | `blender` when a library with characters is configured, else `null`. |

## Character library format

Point **Character → Assets** at either a folder containing `manifest.json`, or at
the `manifest.json` itself. Relative paths inside the manifest resolve against the
manifest's own folder, which is what makes the library portable between an artist
workstation and a Linux render node.

```json
{
  "schema_version": 1,
  "characters": [
    {
      "id": "ch41",
      "name": "Character 41",
      "blend_path": "ch41/character.blend",
      "object_name": "MH_ch41",
      "collection": "CH_ch41",
      "scale": 1.0,
      "offset": [0.0, 0.0, 0.0],
      "rotation_euler_deg": [0.0, 0.0, 0.0],
      "animations": ["Idle", "Cross_Punch"],
      "details": {}
    }
  ],
  "animations": [
    {
      "id": "Cross_Punch",
      "blend_path": "ch41/animations.blend",
      "action_name": "Cross_Punch",
      "frame_start": 0,
      "frame_end": 60,
      "loop": true,
      "applies_to": ["ch41"]
    }
  ]
}
```

* `blend_path` — the `.blend` containing the character (relative or absolute).
* `object_name` — preferred root object. If omitted, the adapter picks the
  armature with the most children, else the mesh with the most vertices.
* `collection` — appended first when present, otherwise the whole file is appended.
* `applies_to` — restricts an animation to specific character ids; empty means
  "any character".
* `animations` on a character is informational; the `animations` array is what
  drives the matrix.

A folder with `.blend` files and **no** manifest is scanned and reported as a
degraded library (characters found, no animations), so you can get moving before
writing a manifest.

## What the Blender adapter does

1. **Import.** `bpy.ops.wm.append` for the configured collection or object, falling
   back to a whole-file append. New objects are identified by diffing
   `bpy.data.objects` before/after, so Blender's collision renaming
   (`Armature.001`) cannot confuse it. Records `imported_objects` in the metadata.
2. **Place.** Applies the manifest's `scale` / `rotation_euler_deg` / `offset`,
   centres the character on the scene footprint, then **snaps it to the floor** by
   ray-casting downward on a 5×5 grid over its footprint and using the *median* hit
   height (so a stray prop below the floor does not sink it).
3. **Animate.** Appends the animation library if the action is not already in the
   file, finds the armature among the imported objects, and binds the action —
   including the Blender 4.4+ **action slot**, without which the assignment
   silently has no effect.
4. **Validate.** Reports a degenerate bounding box, an implausibly large scale,
   a character extending outside the scene bounds, a base that does not touch the
   floor, and — by casting outward from 27 probe points — whether the character is
   inside scene geometry.

The character's own meshes are excluded from the camera's clipping/occlusion
checks and from the character-visibility check, so filming a character never
reports the character as an obstruction of itself.

## Adding a new adapter

1. Subclass `CharacterProvider` in `character/`.
2. Implement the abstract methods; return `STATUS_UNAVAILABLE` with a reason for
   anything you cannot do.
3. Register it in `character/library.py::build_provider`.
4. Add its name to `available_provider_names()` so configs validate.

Nothing in `core/` or `camera/` needs to change — they only talk to the interface.

## Tests

* `test_blender_integration.py` — a fake provider proves the `both` mode expands
  to character-free + with-character variants; the `null` provider never claims
  success; a manifest pointing at a missing `.blend` fails honestly instead of
  pretending.
* Character **visibility** validation is tested against real `bpy` geometry (a
  character behind a wall must fail, a free-standing character must pass).
