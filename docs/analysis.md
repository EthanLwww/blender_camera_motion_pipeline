# Reference analysis

Analysis of the two reference projects that informed this implementation. Both
were **read only** — nothing in them was modified.

* `E:\UE\MetaHumanScenePipeline` — Unreal plugin (`mh_scene_pipeline` Python package)
* `E:\UE\DataGenScenes\Plugins\MetaHumanScenePipeline\Templates\camera_motion_templates.json`
  — the motion template document (identical to the copy inside the plugin repo,
  SHA-256 `651D6880…C533A`)
* `E:\VSCode\CameraCtrl\movie_render.py` — the Unreal headless renderer

---

## 1. Motion template document

**Shape.** A flat JSON array of 80 entries, each `{"id": str, "keys": [...]}`.
Every keyframe carries exactly four fields:

```json
{"frame": 0, "location": [0, 0, 0], "rotation": [0, 0, 0], "focal": 35.0}
```

All 80 templates use the same three frame numbers — **0, 40, 80** — so the
document encodes *shape only*; timing (fps, absolute start, total length) has to
come from elsewhere. That is exactly how this pipeline treats it:
`motion.frame_start`, `motion.frame_end`, `motion.frame_scale` and
`motion.unit_scale.fps` are configuration, not template data.

**Families.** 16 families × 5 variants, except `hitchcock` which has 10:

| Family | Variants | What it does |
|---|---|---|
| `dolly_in` / `dolly_out` | 5 each | move along the view axis (±150/300 cm at frame 40/80) |
| `fixed` | 5 | no movement; framing/focal variants only |
| `hitchcock` | 10 | push/pull combined with a focal-length ramp |
| `pan_left` / `pan_right` | 5 each | yaw only (±7.5–60°) |
| `pedestal_down` / `pedestal_up` | 5 each | move along the camera's up axis |
| `roll` | 5 | rotation about the view axis |
| `tilt_down` / `tilt_up` | 5 each | pitch only (±8°) |
| `truck_left` / `truck_right` | 5 each | strafe along the camera's right axis |
| `zoom_in` / `zoom_out` | 5 each | focal only (24 ↔ 85 mm) |

**Axis semantics (derived, then verified).**

* `location` is an offset in the camera's **own** frame — a `dolly_in` with
  `[150, 0, 0]` moves the camera *forward*, not along world +X.
* Unreal's local convention is **X forward, Y right, Z up**.
* `rotation` is `[roll, pitch, yaw]` in degrees. Evidence: `tilt_*` varies index
  1 (±8°), `pan_*` varies index 2 (±60°), `roll_*` varies index 0.
* Units are centimetres for location (push distances of 150–600 read naturally as
  1.5–6 m) and millimetres for `focal` (24–140 mm is a plausible lens range).
* `pan_right` uses **positive** yaw, `pan_left` **negative** — i.e. Unreal's yaw
  is right-handed about +Z.

Blender's camera looks down local **−Z** with **+X** right and **+Y** up, so the
mapping is `[forward, right, up] → (right, up, −forward)` in camera-local space,
and the yaw sign flips. All of this lives in
`motion.unit_scale` (see the README) and is asserted by
`tests/probe_axes.py` and `tests/test_motion_templates.py`.

## 2. Unreal plugin architecture (`mh_scene_pipeline`)

| Module | Size | Role | What was taken from it |
|---|---|---|---|
| `core.py` | 15 KB | config model + validation | Validate the config before doing work; refuse contradictory settings up front. |
| `math3d.py` | 2.5 KB | centimetre/degree/UE-axis maths, independent of Unreal | **The single most useful file.** Confirmed `basis(rotation)` takes `[roll, pitch, yaw]` and that `rotate(local, rotation)` applies a *local* offset; `interpolate_keys` confirms the intended keyframe blending (linear with shortest-arc yaw). This is the basis of `camera/motion_templates.py`, rewritten for Blender's axes. |
| `planning.py` | 21 KB | shot planning, auto-placement | Character/shot variability is a *dimension* of the matrix, not a special case. |
| `sequences.py` | 26 KB | sequence construction | One sequence = one self-describing, independently loadable unit. |
| `rendering.py` | 19 KB | render queue driving | Render settings must be reproducible from the artifacts alone. |
| `quality.py` | 19 KB | "clear motion" render profile | Determinism matters more than cleverness for dataset use. |
| `assets.py` | 26 KB | MetaHuman asset handling | Characters are a *pluggable* concern; see below. |
| `fingerprints.py` | 16 KB | asset identity/fingerprints | Stamp artifacts with generator version + inputs. |
| `cli.py` / `entry.py` | 15/12 KB | command line + entry points | Everything must be reachable headlessly. |

**Config format.** The reference configs (`Examples/new_scene.json`,
`room001_library.json`, …) are a single JSON object with `scene`, `characters`,
`animations`, `camera_templates`, `planning`, `render` and `output` sections, in
**camelCase**, with a `schema_version`. This pipeline keeps that shape and that
convention: `BatchConfig` has `batch`/`motion`/`validation`/`search`/`render`
sections, a `schema_version`, and its parser accepts camelCase first — so a config
in the reference's style loads without editing.

**Sequence output.** The reference organises output as
`<asset_root>/<map>/<sequence>/` with per-sequence JSON + TXT beside the render,
and the add-on's `Saved/MetaHumanPipeline` folder for intermediates. The three
level `scene/motion/sequence` tree here is the direct analogue required by the
brief, with the same "metadata travels with the render" principle. One folder is
added around it: a run writes `<project>/blender_camera_<date>/` holding
`sequence/` (that tree), `scene/` (a copy of every source `.blend`, because a
sequence stores the camera animation rather than a scene copy) and `video/`, plus
the headless renderer and the package it imports — so the folder can be zipped to
a render node as it stands.

## 3. `movie_render.py` (Unreal headless renderer)

| Aspect | Reference behaviour | Adopted here |
|---|---|---|
| CLI design | Environment variables (`OUTPUT_ROOT_DIR`, `MAP_PATH`, `ENCODE_FPS`) plus `TASK_ID`/`TOTAL_TASKS` sharding | Arguments *and* a config file, plus `--workers` for sharding |
| Batch loop | Clone a job into the queue, render, encode, delete, repeat; one bad job aborts | Same sequential idea, but a failure is recorded and the loop continues |
| Output | `<OUTPUT_ROOT>/<level>/<sequence>/` | `<output_root>/<scene>/<motion>/<sequence>/` |
| Video | Render PNGs, then `ffmpeg -crf 18 -preset slow`; delete the PNGs | Blender's own FFmpeg writer (no external dependency); optional `--frames-output` uses `ffmpeg` when present |
| Exit | `unreal.SystemLibrary.quit_editor()` | `bpy.ops.wm.quit_blender()` + a real process exit code |
| Asset remap | Reload the map per job to avoid pose pollution | Path remapping + a missing-asset pre-flight instead |

**JSON details** — the reference writes **`.jsonl`** with one record per sequence:

```json
{"level_name": "...", "sequence_name": "...", "video_id": "...",
 "video_path": "<dir>/<sequence>.####.png", "frame_count": 81,
 "camera_trajectory": [{"frame": 0, "fov": 54.4, "focal_length": 35.0,
                        "matrix": [[...4x4...]]}],
 "text_prompt": ""}
```

Per the brief this pipeline writes a **`.json`** document, and reproduces those
seven keys verbatim — including `level_name` (mapped to the scene name) and the
`text_prompt` placeholder — then adds the pipeline-specific fields
(`has_character`, `sequence_id`, `validation`, `motion`, `search`, `render`, …).
`video_path` points at the final video instead of a PNG pattern.

**Camera trajectory TXT** — the reference writes:

```text
frame focal_length d1 d2 d3 d4 d5 r00 r01 r02 tx r10 r11 r12 ty r20 r21 r22 tz
```

and notes the header exists because `CameraPoseVisualizer` skips the first line.
`d1..d5` are reserved distortion slots left at zero. Each row is the first three
rows of a **world-to-camera** matrix, flattened, produced as `R = [forward, right,
up]`, `T = location`, then `R^T` and `-R^T·T`.

This pipeline writes the identical header and the identical 12-value row layout,
so the file is drop-in compatible. The mapping is documented in every file's `#`
comment block: row 0 = OpenCV/UE +X (right), row 1 = +Y (down), row 2 = +Z
(the camera's `back` axis, exactly as the reference computes it), translations
from `W2C = [R^T | −R^T·c]`, units in Blender world units (metres by default).
`tests/test_camera_validation.py` pins this with a hand-computed lookup table and
the invariant that the camera's own position maps to the camera-space origin.

## 4. Deliberate differences

| Reference | Here | Why |
|---|---|---|
| `unreal` only, MetaHuman assets | Blender, pluggable `CharacterProvider` | MetaHuman blueprints cannot be loaded by Blender; the brief asks for a pluggable module that still runs without one. |
| PNG frames → external `ffmpeg` | Blender FFmpeg writer (ffmpeg optional) | Removes a hard external dependency on a render node; `--frames-output` still offers the two-step path. |
| Abort the batch on the first failure | Record, continue, roll up | A single bad template must not cost a multi-hour batch. |
| No camera validation | Full validation + auto-search | Required by the brief; implemented before any keyframe is written. |
| Re-render everything every run | Resume/skip by inspecting existing videos | Farm runs restart often. |
| Environment variables only | Arguments + config file + env fallbacks | Easier to reproduce and to diff. |
