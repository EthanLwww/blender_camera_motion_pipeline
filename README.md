# Motion Pipeline — Blender add-on + headless renderer

Batch-load `.blend` scenes, generate camera-motion sequences from JSON templates
(with validation and automatic camera repositioning), and render them to video on
a workstation or a headless render node.

Verified against **Blender 5.2.2 LTS** (Python 3.13) on Windows, with an
end-to-end pass over 3 scenes × 4 templates — 20 sequences — 20 MP4/JSON/TXT
triples. The code also handles Blender 4.x and 3.6 API shapes for the features it
uses (see [Version compatibility](#version-compatibility)).

---

## Contents

1. [What it does](#what-it-does)
2. [Installation](#installation)
3. [Using the add-on](#using-the-add-on)
4. [Headless generation (CLI)](#headless-generation-cli)
5. [Headless rendering](#headless-rendering)
6. [Output layout](#output-layout)
7. [Configuration reference](#configuration-reference)
8. [Motion templates](#motion-templates)
9. [Camera validation and auto-search](#camera-validation-and-auto-search)
10. [Characters](#characters)
11. [Remote / render-farm notes](#remote--render-farm-notes)
12. [Testing](#testing)
13. [Architecture](#architecture)
14. [Version compatibility](#version-compatibility)
15. [Known limitations](#known-limitations)

---

## What it does

| Stage | Entry point | Result |
|---|---|---|
| Batch scene loading | panel **or** `motion_pipeline_cli.py` | one `.blend` at a time, originals never modified |
| Motion template parsing | any template JSON | generic parser, no motion type hard-coded |
| Camera validation | always on by default | clipping, penetration, occlusion, framing, jumps |
| Camera auto-search | when validation fails | spherical search + weighted scoring |
| Sequence generation | panel **or** CLI | independent, renderable `.blend` per combination |
| Video rendering | `render/render_sequences.py` | MP4 + JSON + camera-trajectory TXT per sequence |

The matrix that gets generated is
**scene × motion template × camera × character × character animation**, and the
character dimension collapses to a single "no character" entry when character
handling is off.

---

## Installation

### As a Blender add-on

1. Zip the `blender_motion_pipeline` folder (or copy it into your add-ons path):

   ```powershell
   # Windows
   Copy-Item -Recurse blender_motion_pipeline "$env:APPDATA\Blender Foundation\Blender\5.2\scripts\addons\"
   ```

   ```bash
   # Linux
   cp -r blender_motion_pipeline ~/.config/blender/5.2/scripts/addons/
   ```

2. **Edit — Preferences — Add-ons — Motion Pipeline — Enable.**
3. The panel appears in the 3D viewport sidebar: press <kbd>N</kbd> and pick the
   **Motion Pipeline** tab.

The add-on auto-discovers the motion template document on first enable (see
[Motion templates](#motion-templates)) and pre-fills the panel from it.

### For headless use only

Nothing to install — point Blender at the scripts:

```bash
blender -b -P /path/to/blender_motion_pipeline/motion_pipeline_cli.py -- --help
blender -b -P /path/to/blender_motion_pipeline/render/render_sequences.py -- --help
```

Both scripts locate the package relative to themselves, so they work in place or
copied next to the package.

---

## Using the add-on

Press <kbd>N</kbd> in the 3D viewport and open the **Motion Pipeline** tab.

The panels are ordered for the workflow: **Quick actions**, Scenes, Character,
Motion templates, Camera validation, Sequence output, Local render, Actions,
Status.

### Quick actions

The first panel holds just the two buttons for the everyday workflow, with a
line under them showing exactly what each will act on:

| Button | Does |
|---|---|
| **Generate sequences** | The same as `Start generation` below. While a run is active it becomes **Stop generating**. |
| **Render video** | Loads the sequence list automatically when *Sequence root* is set and the list is still empty, then renders every pending sequence. While a render is active it becomes **Stop render**. |

Configure the scene list and the two output folders once, then those two buttons
are the whole workflow.

A panel run is driven by a `bpy.app.timers` callback, and every step may open
another `.blend`. Opening a file **empties Blender's Python timer registry** (the
same file-read path that drops script-registered `load_post` handlers), so each
callback re-registers itself — and the operator and panel-draw paths heal the
timers too. Without that, a run killed its own driver on the first scene it opened
and sat on `opening <scene>` forever while still reporting `running`.

### Renaming the add-on folder

The package is **name-agnostic**: every module inside it uses relative imports, so
the folder may be called `blender_motion_pipeline`, `blender_camera_motion_pipeline`
or anything else without touching the code. The two standalone scripts (the CLI and
the headless renderer) and the test suites are the exception — they are run as
files and must import the package by name, so they load `_bootstrap.py` by path,
which finds the package root and makes the historical name resolve to it. Rename
the folder, re-zip, and both entry points keep working.

### My settings — the configuration is remembered

The panel's settings live on the scene (`scene.mpp`), which is **per file**: a run
opens every queued `.blend`, so the scene you configured gets replaced and the next
one starts from defaults. Measured on this build, **11 of 14 configured fields were
lost** the moment another scene was opened — which is why everything had to be
typed again after each run.

The configuration is now also kept outside the `.blend`, in
`<Blender config>/blender_motion_pipeline/panel_settings.json`, and put back when
the scene changes:

* **Remember these settings** (or just pressing *Generate sequences* / *Start
  generation*, which snapshots before the first scene opens) writes it.
* It is re-applied automatically whenever the active scene changes — opening a
  file, a batch that opens files, even *File > New* — within a second.
* **Use my settings** forces it onto the current scene; the trash icon forgets it.
* A file that **carries its own deliberate configuration keeps it**; only a scene
  that was never configured is seeded, so a carefully set-up `.blend` is never
  silently overwritten.
* Remembered: the whole `BatchConfig` (templates, output, validation, search,
  render defaults…), the panel-only fields (camera selection, browse folders,
  filters), the local-render options and the queued scene list.

Set `MPP_PANEL_SETTINGS` to use a different file (a portable profile, or a
hermetic test run — the test suite points it at a scratch file so it can never
touch yours). `tests/probe_settings_persistence.py` walks the whole flow and prints
what survives.

### Scenes

* **Scene file** + **Add file** — add one `.blend`, or use the file picker to
  multi-select.
* **Folder** + the folder button — scan a directory (optionally recursively).
* The list shows each scene with a status icon; select a row to see its path,
  camera count and detail. **Missing only** filters the list to problems.
* **Remove selected** / **Clear list** manage the queue; **Save list** /
  **Load list** persist it to JSON.
* Duplicates and non-existent files are rejected with a reason, never silently
  added.

### Character

* **Character mode**: `No character` / `Character sequences only` /
  `Both with and without character`.
* **Assets** / **Animations** point at a Blender character library (see
  [Characters](#characters)).
* **Import status** reports what the provider can actually do — an unavailable
  provider is stated plainly and the character-free flow still runs.

### Motion templates

* **Motion templates** — the JSON document. It is **pre-filled on load** with the
  resolved template set path, so the field is never blank: the project's own
  template document wins, and the copy bundled with the add-on (byte-identical to
  the reference file) is only the fallback on a machine that has no template set.
  When the field is empty the panel also shows the discovered default with a
  **Use the default template set** button.
* **Motion filter** — comma-separated ids or globs, e.g. `dolly_*, pan_left_*`.
* **Load motion templates** reloads and reports the count and source.
* **First frame**, **FPS**, **Interpolation** control the timeline.

### Camera validation

* **Validate cameras**, **Sample step**, **Minimum clearance**,
  **Blocked-shot distance**, **Max move/turn per frame**.
* **Check character visibility** / **Check character overlap** plus the minimum
  visible fraction.
* **Auto-adjust camera** enables the spherical search: min/max offset radius,
  candidate count, horizontal/vertical samples, shell-only, retries, random seed,
  whether rotation and focal length may change, and how many positions to accept.

### Sequence output

* **Output folder**, **Save sequence .blend**, **Save validation report**,
  **Overwrite existing**, **Reuse existing sequences**, **Cameras**
  (`all`, names, or indices), and the render defaults recorded for the renderer.
* **Render defaults (recorded for the renderer)** — engine, samples, fps, video
  format, trajectory sampling and the **Sequence resolution**. All of them are
  written into every sequence's `sequence_config.json`, so a later headless render
  reproduces them.
* **Sequence resolution** — a preset list instead of free numbers, each label
  spelling out the pixels: *720p (1280×720)* — the default —, *1080p (1920×1080)*,
  *1K square (1024×1024)*, *2K (2048×1080)*, *4K (3840×2160)*, plus **Follow the
  source scene** (the historical behaviour) and **Custom size** for a size that
  arrived from a config file or `--resolution`. The line under the dropdown says
  what the current choice means. The record is readable from outside too:
  `render.effective_resolution` is the size the sequence is meant to render at,
  next to `render.scene_resolution` (what the source scene had).

Without a preset the renderer keeps whatever resolution the source scene has, which
is how a 2000×2000 scene produced 2000×2000 videos no matter what the panel said.
Resolution precedence for a render, highest first: **command line / panel override**
(`--resolution-x/y`, or the Local render **Override resolution** tick) → **the
sequence's record** (only when the sequence fixes one) → **the loaded scene**. The
render report and the per-sequence render log both name the winner
(`resolution_source: sequence | command line | scene`), so a surprise size is
traceable instead of mysterious.

### Local render

Renders generated sequences to video **without leaving Blender**, and without
touching the file you have open: each sequence is rendered by a background
Blender process running the standalone renderer, so the panel uses exactly the
same code path a render farm does.

* **Sequences** — point **Sequence root** at a generated tree (a scene folder, a
  motion folder, or the whole output root) and press 🔄 to list what is there.
  Or set **Sequence folder** to render one specific sequence. Folders already
  containing a video are listed as `Skipped`.
* The list shows `scene / motion / sequence` with a live per-row state
  (`pending` — `rendering` — `done` / `skipped` / `failed`), and failures show
  their reason in the row.
* Both sequence shapes render from here: a scene-copy sequence uses its own
  `sequence_<id>.blend`, and an animation-only one is marked with a dot in the row
  and is rendered by replaying its stored camera animation onto the source scene.
* **Save to** — where the videos go. Defaults to
  `<parent of Sequence root>/render_output`. **Flat output** writes every
  sequence straight into that folder instead of the
  `scene/motion/sequence` tree; the folder button opens it.
* **Quality** — engine (EEVEE / Cycles / Workbench), resolution / FPS / samples
  with an explicit tick-box override each (unticked = keep the sequence's own
  recorded setting), Cycles device, container and codec, quality preset, and an
  optional PNG sequence beside the video.
* **Check only** validates inputs and outputs without rendering.
* **Render** (selected row) · **All** (every pending row) · **Stop render**
  (terminates the child process) · trash icon clears the list.
* A `panel_render.log` in the save folder records every command and its outcome.

### Actions

`Check configuration` · `Validate scenes` · **`Start generation`** · `Stop task`
· `Open output folder` · `View error report` · `Export configuration` /
`Import configuration` · `Reset to defaults`.

`Start generation` never blocks the UI: it queues the work and a Blender timer
generates one sequence per tick, updating the **Status** panel. `Stop task`
cancels between sequences.

The **Local render** panel's buttons work the same way: it drives background
Blender processes from a timer, so the UI stays responsive and **Stop render**
can terminate the child process.

### Status

Shows the generation task state and the render state side by side, so a run that
replaced the loaded scene still reports its progress (status is kept in the
add-on preferences, which survive a scene change).

---

## Headless generation (CLI)

```bash
# Everything from a config file, scanning a folder of scenes
blender -b -P motion_pipeline_cli.py -- \
    --config batch.json \
    --scene-dir "D:\scenes" --recursive \
    --output-root "D:\generated"

# One scene that Blender already has open
blender -b "D:\scenes\room001.blend" -P motion_pipeline_cli.py -- \
    --include-current --output-root "D:\generated"

# Select templates and narrow the frame range
blender -b -P motion_pipeline_cli.py -- \
    --scenes "D:\scenes\room001.blend" \
    --output-root "D:\generated" \
    --templates "E:\UE\...\camera_motion_templates.json" \
    --motion-filter "dolly_*" --motion-filter "pan_right_01_standard" \
    --frames 1:120 --fps 24

# Report what would happen, without writing anything
blender -b -P motion_pipeline_cli.py -- \
    --config batch.json --scene-dir "D:\scenes" --dry-run

# Check configuration and scenes only
blender -b -P motion_pipeline_cli.py -- --config batch.json --check-only

# Print the effective configuration
blender -b -P motion_pipeline_cli.py -- --print-config
```

Exit codes: `0` success, `1` a generation problem, `2` bad configuration/inputs.

---

## Headless rendering

```bash
# Render the file Blender already has open
blender -b sequence_000001.blend -P render/render_sequences.py -- \
    --output "D:\render_output" --video-format mp4

# Render a whole generated tree
blender -b -P render/render_sequences.py -- \
    --input-root "D:\generated" \
    --output-root "D:\render_output" \
    --recursive

# Narrow the selection
blender -b -P render/render_sequences.py -- \
    --input-root "D:\generated" --output-root "D:\render_output" \
    --scene-filter "room*" --motion-filter "dolly_*" --sequence-filter "sequence_00000[1-5]"

# Check inputs, assets and outputs without rendering
blender -b -P render/render_sequences.py -- \
    --input-root "D:\generated" --output-root "D:\render_output" --dry-run

# List what would be rendered
blender -b -P render/render_sequences.py -- \
    --input-root "D:\generated" --list

# Quality and engine overrides
blender -b -P render/render_sequences.py -- \
    --input-root "D:\generated" --output-root "D:\render_output" \
    --engine CYCLES --device GPU --samples 128 \
    --resolution-x 1920 --resolution-y 1080 --fps 24 \
    --video-format mp4 --codec H264 --crf HIGH

# Nothing specified? The sequence's own record is used: engine/samples always,
# resolution when the sequence fixes one (see "Sequence output").
blender -b -P render/render_sequences.py -- \
    --input-root "D:\generated" --output-root "D:\render_output"

# Map asset paths stored on the authoring machine onto the render node
blender -b -P render/render_sequences.py -- \
    --input-root /mnt/gen --output-root /mnt/out \
    --path-map "E:\scenes=/mnt/e/scenes" --path-map "E:\textures=/mnt/e/textures" \
    --asset-report /mnt/out/assets.json

# Parallel workers (one Blender process per batch)
blender -b -P render/render_sequences.py -- \
    --input-root "D:\generated" --output-root "D:\render_output" --workers 4
```

`--workers` starts one Blender process per batch, and each worker command is
built as `blender -b -P render_sequences.py -- --input ...` — Blender parses its
own options up to the standalone `--`, so anything before it (an `--input` that
belongs to the script) is read as a file name and the worker silently renders
nothing.

Workers only pay off when the bottleneck is per-process rather than per-GPU.
EEVEE renders one frame on the GPU, so several workers on one GPU compete for the
same VRAM: on a heavy scene (3.3 M polygons, 34 shadow-casting lights, 4.2 GB
resident) three workers made each frame ~10x slower instead of ~3x more
throughput. Measure before fanning out; `--workers 1` is the fastest option on a
single-GPU machine for scenes like that.

Exit codes: `0` success (including "everything was already rendered"),
`1` at least one sequence failed, `2` no sequences found, `3` unexpected error.

Supported flags: `--input`, `--input-root`, `--output`, `--output-root`,
`--recursive`, `--scene-filter`, `--motion-filter`, `--sequence-filter`,
`--frame-start`, `--frame-end`, `--resolution-x`, `--resolution-y`,
`--resolution-percentage`, `--fps`, `--engine`, `--samples`, `--device`,
`--video-format`, `--codec`, `--crf`, `--video-bitrate`, `--trajectory-mode`,
`--trajectory-step`, `--overwrite`, `--skip-existing`, `--no-skip-existing`,
`--dry-run`, `--list`, `--workers`, `--keep-frames`, `--frames-output`,
`--log-level`, `--log-file`, `--config`, `--path-map`, `--check-assets`,
`--no-check-assets`, `--asset-report`, `--flat`, `--timeout`.

---

## Output layout

### Generated sequences

```text
D:\generated\
├── batch_config.json effective configuration for this run
├── batch_report.json per-scene and per-sequence outcome
├── manifest.json roll-up of every sequence + every failure
└── room001\
    └── dolly_in_01_standard\
        ├── manifest.json
        ├── sequence_000001\
        │   ├── sequence_000001.blend      independent, renderable scene
        │   ├── sequence_config.json       what the generator decided
        │   ├── sequence_000001.json       camera trajectory + metadata
        │   ├── sequence_000001_camera.txt camera trajectory, one row per frame
        │   ├── validation_report.json     per-frame metrics + search log
        │   └── generation_log.txt         step-by-step log for this sequence
        └── sequence_000002\
            └── ...
```
Sequence numbering restarts at `sequence_000001` inside each motion folder, so a
motion folder is self-contained and re-running one motion never renumbers another.

`sequence_<id>.blend` is a **complete copy of the scene** (that is what makes a
sequence folder renderable on its own, anywhere, without the original file) plus
the generated camera action, so it is written **compressed** — the animation
itself is negligible next to the geometry and packed textures:

| | measured on the 265.9 MB reference scene |
|---|---|
| scene source | 265.9 MB (already compressed) |
| sequence blend, `compress=False` | 610.6 MB |
| sequence blend, `compress=True` (default) | 264.9 MB |
| animation only (no scene copy) | ≈ 150 KB |
| packed textures carried by every scene copy | 172.3 MB |
| geometry (2.29 M vertices / 3.3 M polygons) | the rest |
| camera + its generated action alone | ≈ 5.4 MB as a `.blend`, ≈ 28 KB as JSON |

Compression is not free: on that scene a 17-sequence batch took **100 s**
uncompressed versus **349 s** compressed (zstd runs single-threaded while the
writes are 2.3x smaller). If you would rather spend disk than time, sets
`compress=False` — `tests/probe_blend_size.py` prints all of the numbers above for
any scene of yours.

Everything but the blend is tiny (`sequence_<id>.json` ~110 KB, trajectory TXT
13 KB), so the *only* thing that scales with sequence count is the scene copy per
sequence. If you generate many sequences from one scene and would rather not
duplicate it, turn the copy off with `--no-sequence-blend` (or the panel's **Save
sequence .blend** toggle). The sequence then stores the camera animation instead of
the scene:

| | per sequence | 17 sequences |
|---|---|---|
| scene copy (`save_sequence_blend=True`, default) | 264.9 MB (compressed) | 4.4 GB |
| animation only (`--no-sequence-blend`) | **~150 KB** | **~2.5 MB** + one shared source scene |

An animation-only sequence still renders: the renderer opens the source scene
recorded in `sequence_config.json` (`sequence.source_blend`) and replays the keyed
camera animation onto it from `sequence_<id>.json` →
`camera_animation.samples`. The payload records the values that were actually
keyed — `location` in parent space, `rotation_quaternion`, `scale` and the camera
data's `lens`, per frame — plus the interpolation and which constraints were muted
for the bake, so replaying cannot drift from what the generator validated.
`tests/probe_animation_only_equivalence.py` proves the two shapes agree: on the
reference scene the same template rendered through both storage modes differs by
`1e-4` in the W2C matrix, i.e. single-precision residue on a coordinate 431 m from
the origin, and the animation-only path is the *more* accurate of the two when
compared against the generator's own trajectory.

The trade-off is self-containment: a blend-based sequence folder renders anywhere
on its own, an animation-only one needs the source scene reachable from the render
node (`--path-map` remaps asset paths, and the same applies here). `--list` shows
which shape each sequence uses (`[blend]` / `[animation]`), and
`tests/probe_blend_size.py` prints the numbers for your own scene.

### Rendered videos

```text
D:\render_output\
├── render_report.json
└── room001\
    └── dolly_in_01_standard\
        └── sequence_000001\
            ├── sequence_000001.mp4
            ├── sequence_000001.json          JSON details
            ├── sequence_000001_camera.txt    camera trajectory
            ├── sequence_config.json          copied from the source sequence
            └── sequence_000001_render_log.txt
```
### JSON details file

The first block reproduces the keys written by the Unreal reference script
(`E:\VSCode\CameraCtrl\movie_render.py`) so existing consumers keep working; the
second block adds the video-render details:

```json
{
  "level_name": "room001",
  "sequence_name": "sequence_000001",
  "video_id": "sequence_000001",
  "video_path": "D:/render_output/room001/dolly_in_01_standard/sequence_000001/sequence_000001.mp4",
  "frame_count": 81,
  "camera_trajectory": [
    {"frame": 0, "fov": 54.43, "focal_length": 35.0,
     "matrix": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]}
  ],
  "text_prompt": "",

  "sequence_id": "sequence_000001",
  "scene_name": "room001",
  "motion_name": "dolly_in_01_standard",
  "camera_name": "Camera",
  "has_character": false,
  "character_name": "",
  "character_animation": "",
  "source_blend": "D:/scenes/room001.blend",
  "status": "rendered",
  "error": "",
  "frames": {"frame_start": 0, "frame_end": 80, "frame_count": 81, "fps": 24.0},
  "render": {"engine": "BLENDER_WORKBENCH", "resolution": [320, 180], "fps": 24.0},
  "trajectory_export": {
    "coordinate_system": "opencv_world_to_camera",
    "rotation_representation": "3x3 rotation matrix rows r00..r22",
    "units": "blender_world_units (metres by default)",
    "distortion_slots": "d1..d5 are reserved and always 0"
  }
}
```

### Camera trajectory TXT

Header line and one row per frame, byte-compatible with the reference script
(which skips the `#` comment block):

```text
# sequence_id=sequence_000001
# scene=room001 motion=dolly_in_01_standard camera=Camera
# frames=0..80 fps=24
# coordinate_system=opencv_world_to_camera (row0=+X right, row1=+Y down, row2=+Z)
# rotation_representation=3x3 rotation matrix, rows r00..r22, column-vector convention
# translation=tx ty tz from W2C = [R^T | -R^T * camera_position]
# units=blender_world_units (metres by default); distortion d1..d5 reserved, always 0
frame focal_length d1 d2 d3 d4 d5 r00 r01 r02 tx r10 r11 r12 ty r20 r21 r22 tz
0 35 0 0 0 0 0 0.00000006 -1.00000000 ... -0.00000010
```

`--trajectory-mode sampled --trajectory-step N` exports every N-th frame
(the first and last frame are always included).

---

## Configuration reference

One JSON document drives both the panel and the CLI. See
[`config/example_config.json`](config/example_config.json) and the formal
[`config/schema.json`](config/schema.json).

```json
{
  "schema_version": 1,
  "batch": {
    "output_root": "D:/generated_sequences",
    "mode": "none",
    "scene_name_mode": "stem",
    "overwrite": false,
    "resume": true,
    "save_sequence_blend": true,
    "save_validation_report": true,
    "character_asset_root": "",
    "animation_asset_root": "",
    "character_provider": "auto",
    "path_mappings": [{"from": "E:/UE", "to": "/mnt/e/UE"}]
  },
  "motion": {
    "template_path": "E:/UE/.../camera_motion_templates.json",
    "template_names": [],
    "template_overrides": {"dolly_in_03_strong": {"location_scale": 0.5}},
    "frame_start": 0,
    "frame_end": null,
    "frame_scale": 1.0,
    "interpolation": "BEZIER",
    "unit_scale": {
      "location_scale": 0.01,
      "fps": 24.0,
      "rotation_order": "XYZ",
      "yaw_axis": "Z", "pitch_axis": "X", "roll_axis": "Z",
      "yaw_sign": -1.0, "pitch_sign": 1.0, "roll_sign": -1.0,
      "location_forward": -1.0, "location_right": 1.0, "location_up": 1.0
    }
  },
  "validation": {
    "enabled": true, "sample_step": 10, "clearance": 0.25,
    "obstruction_distance": 1.0, "max_position_jump": 2.0,
    "max_rotation_jump_deg": 45.0, "jump_gap_scale": "sqrt",
    "check_character_visibility": true, "min_character_visible_ratio": 0.05,
    "check_character_overlap": true
  },
  "search": {
    "enabled": true, "min_radius": 0.2, "max_radius": 3.0,
    "candidate_count": 64, "azimuth_samples": 12, "elevation_samples": 5,
    "max_retries": 2, "random_seed": 1234,
    "allow_rotation_adjust": true, "max_rotation_adjust_deg": 25.0,
    "allow_focal_adjust": true, "focal_adjust_steps": 3.0,
    "max_output_candidates": 1
  },
  "render": {
    "engine": "BLENDER_EEVEE", "samples": 32,
    "resolution_x": 1280, "resolution_y": 720, "fps": 24.0,
    "video_format": "mp4", "codec": "H264", "constant_rate_factor": "HIGH",
    "trajectory_mode": "all_frames", "trajectory_step": 1
  },
  "scenes": [{"path": "E:/scenes/room001.blend", "enabled": true}]
}
```

Keys are accepted in `snake_case`, `camelCase`, `kebab-case` or `UPPER_CASE`.
Unknown keys produce a warning instead of failing, so a config written for a
newer build still runs.

---

## Motion templates

The generator reads any document shaped like the reference file — nothing about a
motion type is hard-coded in Python, so adding a motion means adding a JSON entry.

### Discovery order

1. `motion.template_path` (panel field / `--templates`) — an error if unreadable.
2. `motion.template_data` (inline array in the config).
3. `$MOTION_PIPELINE_TEMPLATES`.
4. The bundled `config/camera_motion_templates.json`, then the known reference
   locations:
   `E:\UE\DataGenScenes\Plugins\MetaHumanScenePipeline\Templates`,
   `E:\UE\MetaHumanScenePipeline\Templates`, — a warning only, falling back to
   an embedded minimal set.

### Accepted shapes

```json
[{"id": "dolly_in_01_standard",
  "keys": [{"frame": 0,  "location": [0, 0, 0],   "rotation": [0, 0, 0], "focal": 35},
           {"frame": 40, "location": [150, 0, 0], "rotation": [0, 0, 0], "focal": 35},
           {"frame": 80, "location": [300, 0, 0], "rotation": [0, 0, 0], "focal": 35}]}]
```

`{"templates": [...]}`, `{"motion_templates": [...]}`, `{name: {...}}` and a
single template object are all accepted, as are the aliases `name`/`template` for
`id`, `keyframes`/`samples`/`frames` for `keys`, `position`/`pos` for `location`,
`angles`/`rot` for `rotation`, and `lens`/`focal_length` for `focal`. Extra
per-keyframe or per-template fields are preserved in `parameters`.

### Coordinate contract

Template `location` is an offset in the camera's **own frame**, using Unreal's
axis convention (**X = forward, Y = right, Z = up**) and centimetres, and
`rotation` is `[roll, pitch, yaw]` in degrees. `motion.unit_scale` maps that onto
Blender (`-Z` forward, `+X` right, `+Y` up):

| Template | Becomes | Default |
|---|---|---|
| location scale | centimetres — metres | `0.01` |
| `location_forward` | Unreal +X — Blender **−Z** | `-1` |
| `location_right` | Unreal +Y — Blender **+X** | `+1` |
| `location_up` | Unreal +Z — Blender **+Y** | `+1` |
| `yaw` | about world **+Z** | sign `-1` |
| `pitch` | about the camera's local **X** | sign `+1` |
| `roll` | about the camera's local **Z** (the view axis) | sign `-1` |

Every one of those is asserted by
[`tests/probe_axes.py`](tests/probe_axes.py) and locked down in
`tests/test_motion_templates.py` — with the defaults, `dolly_in` really does push
the camera forward, `pan_right` really does turn it right, `pedestal_up` really
does raise it, `truck_right` really does strafe it right, and `roll` really does
spin the frame without changing the aim.

### What gets keyed: the bake contract

The generator builds **world-space** poses — that is what the validator checks and
what the trajectory JSON/TXT record — and the bake has to reproduce exactly those
poses in the space Blender evaluates the camera in. `obj.location` is expressed in
the object's **parent** space, so a parented camera is converted per frame:

```
local_basis = inverse(matrix_parent_inverse) @ inverse(parent_world) @ world_pose
```

`parent_world` is re-read from the evaluated depsgraph at every keyframe, because
the parent's own animation is what carries a rig-mounted camera through the scene.
Constraints with a non-zero influence are muted for the bake (and left muted): a
keyed rotation cannot survive an active `TRACK_TO`/`COPY_ROTATION`, so leaving one
live would make the render disagree with the validated path.

The consequence worth knowing: the generated motion **replaces** the camera's own
animation with the template path anchored at the camera's start pose. On a camera
parented to a moving rig, the rendered camera therefore holds that world path
instead of inheriting the rig's travel — the subject moves through frame rather
than the camera riding along. The trajectory files, the validation report and the
video all describe the same path, which is the property the renderer depends on.

[`tests/probe_all_sequences.py`](tests/probe_all_sequences.py) sweeps a whole
generated tree and fails if any sequence's evaluated camera path deviates from its
own trajectory by more than 1 cm;
[`tests/probe_bake_math.py`](tests/probe_bake_math.py) checks the matrix algebra
against `mathutils` for random rigs, parent offsets and poses.

### Where a sequence is anchored

Every sequence is anchored on the camera's pose at **its own first frame**
(`motion.frame_start`, or the scene's start frame when unset), and the scene is put
back to the artist's state afterwards — transform, lens *and* action. Both halves
matter:

* The anchor frame is set explicitly before each sequence. Left to the file's
  current frame, the first sequence of a batch would anchor wherever the artist
  saved the file and the rest wherever the previous sequence stopped, so the same
  template would produce a different shot depending on batch position.
* The restore assigns the snapshot's **world** matrix and lets Blender derive the
  local one, then re-attaches the artist's action. Substituting the world
  translation into `obj.location` (parent space) instead looks harmless and is not:
  on the reference scene it walked the camera 25.96 m further along the animated
  train on *every* sequence.

`tests/probe_anchor_drift.py` generates a few sequences in one process and prints
the camera state around each one, which is how both failures were found;
`test_blender_integration` pins them.

### Overriding a template

```json
"template_overrides": {
  "dolly_in_03_strong": {"location_scale": 0.5, "frame_scale": 2.0},
  "*": {"frame_offset": 10}
}
```

Supported patch keys: `keys` (replace wholesale), `frame_scale`, `frame_offset`,
`location_scale`, `focal_scale`, `focal`; anything else is stored as a template
parameter.

### Supported templates

Whatever the document contains. The reference document has **80** templates
across 16 families: `dolly_in`, `dolly_out`, `fixed`, `hitchcock`, `pan_left`,
`pan_right`, `pedestal_down`, `pedestal_up`, `roll`, `tilt_down`, `tilt_up`,
`truck_left`, `truck_right`, `zoom_in`, `zoom_out` (5 variants each, except
`hitchcock` which has 10). `--motion-filter` / the panel's **Motion filter**
selects a subset by id or glob.

---

## Camera validation and auto-search

Validation samples the **first frame, the last frame, every `sample_step`-th
frame, and any `validation.extra_sample_frames`** — never just the start frame.

| Check | Field / reason |
|---|---|
| Camera body clear of geometry | `clearance`, reason `camera_clipping` |
| Camera inside a mesh | `inside_epsilon`, reason `camera_inside_geometry` |
| Camera enclosed / shot blocked | `obstruction_distance`, reason `camera_obstructed` |
| Character framed and visible | `check_character_visibility`, `min_character_visible_ratio`, reasons `character_invisible` / `character_unframed` |
| Character inside geometry | `check_character_overlap`, reason `character_overlap` |
| Abnormal position/rotation jump | `max_position_jump`, `max_rotation_jump_deg`, reasons `position_jump` / `rotation_jump` |
| Non-finite / degenerate values | reason `illegal_value` |
| Clip planes vs scene extent | `min_clip_start`, `max_clip_end`, reason `clip_range_invalid` |

Rays come from `scene.ray_cast` on the evaluated depsgraph, so armature
deformation and modifiers are respected. The character's own meshes are excluded
from *both* the camera-clearance test and the character-visibility test — a
camera is not clipped by the person it is filming, and a character must not
shadow the wall behind it.

`jump_gap_scale` (`sqrt` by default) relaxes the jump limits by
`sqrt(frame_gap)` when the sampling interval spans several frames, so a coarse
`sample_step` does not report a legitimate multi-frame move as a per-frame jump.

### Automatic camera search

When validation fails and `search.enabled` is true, candidate positions are drawn
from a sphere around the original camera:

* a regular azimuth/elevation grid,
* a Fibonacci sphere,
* concentric radial shells,
* seeded random fill,
* optional small orientation nudges (aimed at the character, plus a −adder),
* optional focal-length steps.

Candidates are ranked by

```text
penalty = w_distance   · offset / 10 m
        + w_clipping   · (clipping + inside + overlap + jump + illegal + clip_range ratios)
        + w_invisible  · invisible-frame ratio
        + w_occlusion  · occluded-frame ratio
        + w_rotation   · rotation change / 90°
        + w_focal      · focal change / 100 %
```

The most conservative candidate (least drift) that passes every hard check wins,
and only then are keyframes baked. If nothing passes, the combination is recorded
as a **failure** with a `failure_report.json` — a knowingly bad sequence is never
emitted. With `search.enabled = false`, a failing validation is still recorded as
a failure (this was a real bug during development and is covered by a test).

---

## Characters

`CharacterProvider` is the pluggable adapter the brief asked for:

```python
class CharacterProvider(abc.ABC):
    def status(self) -> str: ...
    def list_characters(self) -> list[CharacterDescriptor]: ...
    def list_animations(self) -> list[AnimationDescriptor]: ...
    def import_character(self, scene_context, character_config) -> CharacterPlacement: ...
    def place_character(self, placement, scene_context) -> CharacterPlacement: ...
    def apply_animation(self, placement, animation_config) -> CharacterPlacement: ...
    def validate_character_placement(self, placement, scene_context) -> CharacterValidation: ...
```

| Provider | State | Notes |
|---|---|---|
| `blender` | **available** | Appends Blender-native character `.blend` libraries, binds armature actions (Blender 4.4+ action slots handled), grounds the character on the scene floor, and validates placement. |
| `null` | unavailable by design | Full interface, no assets. Reports *why* and lets the character-free flow run. Never fakes success. |
| `unreal_metahuman` | not implemented | Documents the platform boundary (MetaHuman blueprints cannot be loaded by Blender without a separate retarget/export step). |
| `auto` (default) | picks `blender` when a library exists, else `null` | |

### Character library manifest

```json
{
  "schema_version": 1,
  "characters": [
    {"id": "ch41", "blend_path": "ch41/character.blend",
     "object_name": "MH_ch41", "collection": "CH_ch41",
     "animations": ["Idle", "Cross_Punch"]}
  ],
  "animations": [
    {"id": "Cross_Punch", "blend_path": "ch41/animations.blend",
     "action_name": "Cross_Punch", "frame_start": 0, "frame_end": 60,
     "applies_to": ["ch41"]}
  ]
}
```

Point **Character — Assets** at the folder containing `manifest.json` (or at the
file itself). Paths inside the manifest resolve relative to the manifest, so the
library stays portable. A folder with `.blend` files and no manifest is scanned
and reported as a degraded (characters, no animations) library.

**Current status: the character module is fully wired and tested against fakes,
but no real character asset exists in this workspace, so character sequences have
not been produced from real MetaHuman or Blender rigs here.** See
[Known limitations](#known-limitations).

---

## Remote / render-farm notes

* No GUI, no clicking: everything comes from arguments or a config file.
* `--dry-run` resolves inputs, outputs and assets without rendering.
* `--asset-report` writes a missing-asset list; `--path-map FROM=TO` rewrites
  stored asset paths to the node's layout, and the remap is logged per asset.
* Rendering is resumable: a finished video is detected (including Blender's
  `<name>_<start>-<end>.mp4` naming, which is normalised to `<name>.mp4`) and
  skipped unless `--overwrite`.
* A fully rendered tree exits `0` with an explicit "nothing to do" message.
* `render_report.json` summarises rendered / failed / skipped per sequence; a
  non-zero exit code tells the scheduler something failed.
* `--workers N` fans out to N Blender processes, one batch each.
* `--device CPU|GPU`, `--samples`, `--engine` cover GPU/CPU selection.
* Windows and Linux paths both work: artifacts are written with forward slashes,
  `--path-map` is separator-insensitive, and stored absolute paths are kept
  verbatim for traceability.

---

## Testing

```bash
# Everything (pure suites + Blender suites) — 187 cases
blender -b -P blender_motion_pipeline/tests/run_blender_tests.py

# Pure suites only, no Blender required (97 cases)
python blender_motion_pipeline/tests/run_blender_tests.py

# Individual suites
blender -b -P blender_motion_pipeline/tests/test_blender_integration.py
blender -b -P blender_motion_pipeline/tests/test_animation_api.py
python blender_motion_pipeline/tests/test_motion_templates.py

# Axis-convention sanity probe (prints what each motion family actually does)
python blender_motion_pipeline/tests/probe_axes.py

# Background MP4 rendering smoke test
blender -b -P blender_motion_pipeline/tests/smoke_render.py

# Full end-to-end acceptance run through the CLI + renderer
blender -b -P blender_motion_pipeline/tests/verify_end_to_end.py

# Bake algebra vs mathutils, for random rigs / parent offsets / poses
blender -b -P blender_motion_pipeline/tests/probe_bake_math.py

# Does a batch anchor every sequence on the same camera pose?
blender -b -P blender_motion_pipeline/tests/probe_anchor_drift.py -- "<scene.blend>" ["<templates.json>" [count]]

# Does the panel configuration survive a run that opens other scenes?
blender -b -P blender_motion_pipeline/tests/probe_settings_persistence.py

# Are every icon identifier the UI passes to ``label(icon=...)`` valid here?
blender -b -P blender_motion_pipeline/tests/probe_icons.py

# Does every sequence in a generated tree render the path it recorded?
blender -b -P blender_motion_pipeline/tests/probe_all_sequences.py -- "<sequence root>"

# What makes EEVEE slow on a given sequence (raytracing, lights, polygons)
blender -b -P blender_motion_pipeline/tests/probe_render_cost.py -- "<sequence.blend>"

# Where the bytes in a sequence .blend come from, and what each storage choice costs
blender -b -P blender_motion_pipeline/tests/probe_blend_size.py -- "<scene.blend>" "<sequence.blend>"

# Do both storage modes (scene copy / animation only) give the same camera path?
blender -b -P blender_motion_pipeline/tests/probe_animation_only_equivalence.py -- \
    "<sequence.blend>" "<animation-only sequence dir>"
```

`MP_KEEP_TEST_OUTPUT=1` keeps the integration artifacts for inspection;
`MP_KEEP_E2E=1` keeps the end-to-end tree; `MP_TEST_TRACEBACK=1` prints full
tracebacks.

### Results on this machine (Blender 5.2.2 LTS, Windows)

| Suite | Cases | Result |
|---|---|---|
| `test_path_utils` | 16 | pass |
| `test_config` | 18 | pass |
| `test_motion_templates` | 30 | pass |
| `test_camera_validation` | 37 | pass |
| `test_animation_api` | 10 | pass |
| `test_addon_lifecycle` | 10 | pass |
| `test_render_workflow` | 18 | pass |
| `test_blender_integration` | 48 | pass |
| **Total** | **187** | **pass** |

`tests/static_check.py` also reports no unused imports or leftover debug markers
across all 74 Python files.

End-to-end acceptance: **9/9 stages**

1. Build 3 fixture scenes.
2. CLI dry-run reports the matrix (20 sequences).
3. CLI generates 20 sequences with the real 80-template document.
4. Generated artifacts match the documented layout (20 checked).
5. Headless render produces 20 videos.
6. Every video has its JSON + trajectory TXT beside it.
7. `ffprobe` confirms H.264 and 81 frames per video.
8. Re-running the renderer skips finished sequences and exits 0.
9. `--list` enumerates sequences and their state.

The add-on was also installed into a real Blender add-ons folder and verified to
enable, expose all 19 operators and 8 panels, discover and pre-fill the template
document (80 templates), and disable cleanly.

Coverage includes the brief's edge cases: missing scene path, scene without a
camera, multiple cameras, missing/malformed template JSON, geometry blocking the
lens, every candidate failing, character assets missing, unreadable output
targets, pre-existing sequences, background execution, and missing external
assets.

---

## Architecture

```text
blender_motion_pipeline/
├── __init__.py add-on entry (bl_info + register/unregister)
├── registration.py registration order, reload, preference defaults
├── properties.py scene PropertyGroup  <->  BatchConfig
├── operators.py the panel operators (+ the generation/render timers)
├── panels.py sidebar panels and the two UILists
├── preferences.py add-on preferences, durable task status
├── motion_pipeline_cli.py headless generation CLI
├── config/
│   ├── models.py typed config dataclasses, lenient parsing, validation
│   ├── defaults.py defaults + template discovery
│   ├── schema.json        JSON schema
│   ├── example_config.json
│   └── camera_motion_templates.json bundled fallback set
├── core/                  orchestration (needs bpy)
│   ├── scene_loader.py discovery, de-duplication, safe loading
│   ├── blender_context.py camera snapshots, geometry harvest, restore
│   ├── sequence_generator.py one sequence: validate -> search -> bake -> write
│   ├── batch_runner.py scenes x motions x cameras x characters
│   ├── sequence_manager.py read-only view of the output tree
│   └── ui_task.py incremental, cancellable timer state machine
├── camera/                camera maths and checks
│   ├── motion_templates.py parser, interpolation, keyframe generation
│   ├── scene_context.py     MeshSnapshot / CameraSnapshot / ray casters
│   ├── camera_validator.py clipping, occlusion, framing, jumps, scoring
│   ├── camera_search.py candidate generation, aim, spherical search
│   └── camera_export.py trajectory TXT + JSON sidecars
├── character/             pluggable character support
│   ├── base_provider.py interface, descriptors, variant expansion
│   ├── null_provider.py unavailable provider (honest)
│   ├── blender_provider.py real .blend character adapter
│   └── library.py manifest loading, provider selection
├── io/
│   ├── path_utils.py cross-platform paths, mappings, sanitising
│   ├── json_io.py         UTF-8/BOM-tolerant, atomic JSON writes
│   ├── manifest.py scene/motion/batch manifests
│   └── resource_check.py missing-asset scanning
├── render/
│   ├── render_sequences.py standalone headless renderer
│   ├── render_runner.py panel render driver (child Blender processes)
│   └── metadata_exporter.py JSON + trajectory writers for the renderer
├── utils/
│   ├── logging_utils.py console + file + UI-callback logging
│   ├── task_control.py cancellation and progress
│   ├── animation.py version-agnostic Action/F-Curve access
│   └── version.py version stamping
└── tests/                 harness + suites + probes (see Testing)
```
Design rules the code actually follows:

* **UI-free core.** Everything under `camera/`, `io/`, `config/`, `utils/` and
  most of `core/` imports no `bpy`, so it is unit-testable and reusable from the
  CLI. `bpy` is imported lazily inside functions where it is needed.
* **Validation before mutation.** Candidates are scored in memory; keyframes are
  baked only for the winner.
* **Originals are never modified.** Cameras are animated on a *copy* of the
  camera data block, the artist's action is left on the original file, and
  saving is always *Save As* into the output tree.
* **Failures are data.** Every failure becomes a `failure_report.json`, a
  manifest entry and a batch-report row; the run continues.
* **Honest status.** When a module cannot do its job (`null` character provider,
  missing ray caster, unreadable template) it says so and the rest of the
  pipeline carries on.

---

## Version compatibility

Built and verified on Blender 5.2.2. The following API changes are handled
explicitly, each with a test:

| Change | Handling |
|---|---|
| `image_settings.file_format` gated behind `media_type` (5.2+) | `set_output_media_type()` sets `VIDEO`/`IMAGE` when the attribute exists |
| `Action.fcurves` removed in favour of layered actions (5.0+) | `utils/animation.py` walks `layers[].strips[].channelbags[].fcurves` with a legacy fallback |
| Action slots required for an action to take effect (4.4+) | `assign_action()` binds `action_slot` when present |
| `scene.ray_cast` accessed through the depsgraph | `BlenderRayCaster` uses `depsgraph` and `evaluated_get` |
| Module operators not injected into `bpy.ops` (5.2) | code and tests use `bpy.ops.mpp.<name>` |
| `PropertyGroup` classes not exposed as `bpy.types` attributes (5.2) | tests assert via `bpy.types.Scene.bl_rna.properties["mpp"].fixed_type` |
| No `Render Result` buffer in `blender -b` | harmless for FFmpeg output; a diagnostic helper is provided |
| Blender appends the frame range to video filenames | the renderer renames `<id>_<start>-<end>.mp4` to `<id>.mp4` |

Requires Blender **3.6+**; verified on **5.2.2**. No third-party Python packages.

---

## Known limitations

1. **No real character assets here.** The character adapter is complete and
   tested against fakes, and the `null`/`unreal_metahuman` paths are exercised
   for real, but no MetaHuman or Blender rig exists in this workspace, so
   character sequences have not been produced from a real asset. MetaHuman
   assets cannot be loaded by Blender without a separate retarget/export step — that is a platform boundary, not a bug in this pipeline.
2. **Geometry tests are ray- and AABB-based.** Thin single-sided planes are hit
   reliably head-on but can be grazed; very dense meshes make the search slower.
   Clearance is sampled along 26 directions, not evaluated analytically.
3. **`sequence.blend` is written once per sequence from the batch/CLI path.** The
   *panel's* timer-driven run cannot call `save_as_mainfile` (Blender crashes with
   an access violation when the file writer re-enters the main loop from a timer),
   so panel runs queue those writes and flush them at the end — only the last
   processed scene state is available then. For exact per-sequence blends, use the
   CLI; turning **Save sequence .blend** off is also a first-class choice now: the
   sequence stores the camera animation instead and the renderer replays it onto
   the source scene (see [Generated sequences](#generated-sequences)).
4. **Resolution is a render-time decision.** Generation does not resize the
   scene; it records resolution/fps/engine in `sequence_config.json` and the
   renderer applies them. Frame *rate* is written to the sequence file
   (`scene.render.fps`) because template frame numbers are absolute.
5. **Focal length is the lens control.** Blender cannot express focal-in-units
   (`lens_unit='FOV'`), so all focal handling is in millimetres; a template focal
   below Blender's 1 mm minimum is clamped, and the requested value is preserved
   in the metadata.
6. **Trajectory sign convention.** The TXT matches the reference's `R^T`
   convention, so a point *in front of* the camera has positive `z` (the reference
   writes the `back` column). This is documented in every file header and pinned
   by a test.
7. **`--workers` runs sequential Blender processes**, not a distributed
   scheduler. Multi-machine distribution is out of scope.
8. **Multi-scene `.blend` files use the active scene.** Other scenes in the same
   file are listed by `Validate scenes` but not generated.
9. **Progress is per sequence, not per frame.** A single very long sequence does
   not report intermediate progress, and `Stop task` waits for it to finish.
10. **A generated motion replaces the camera's own animation**, anchored at the
    camera's start pose (see [the bake contract](#what-gets-keyed-the-bake-contract)).
    On a camera parented to a moving rig the camera holds the template's world
    path rather than riding the rig, so the subject can travel through frame. That
    is self-consistent — trajectory, validation and render agree — but it is not
    the same shot as "a dolly move added to the rig's existing move".
11. **EEVEE cost is the scene's, not the add-on's.** A scene with 34 shadow-casting
    lights and 3.3 M polygons renders at roughly 23 s/frame at 1280x720 with 32
    samples on an RTX 4060 Laptop (~99% GPU utilisation, 4.2 GB VRAM resident, so
    it is GPU-bound rather than misconfigured). Budget render time from
    `tests/probe_render_cost.py` before starting a batch.

---

## License / attribution

Motion template semantics are derived from the Unreal reference project
(`E:\UE\MetaHumanScenePipeline`, `E:\VSCode\CameraCtrl\movie_render.py`), which was
read for analysis only and never modified.
