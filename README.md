# Motion Pipeline — Blender add-on + headless renderer

Batch-load `.blend` scenes, generate camera-motion sequences from JSON templates
(with validation and automatic camera repositioning), and render them to video on
a workstation or a headless render node.

> **Chinese (condensed) version**: [`README.zh-CN.md`](README.zh-CN.md) — same
> content, condensed; panel names, field names, flags and file names stay English.

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
9. [Compound shots](#compound-shots)
10. [Camera region](#camera-region)
11. [Focus objects](#focus-objects)
12. [Camera validation and auto-search](#camera-validation-and-auto-search)
13. [Characters](#characters)
14. [Remote / render-farm notes](#remote--render-farm-notes)
15. [Testing](#testing)
16. [Architecture](#architecture)
17. [Version compatibility](#version-compatibility)
18. [Known limitations](#known-limitations)

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
| Focus objects (optional) | panel **Focus object** | a model placed on the scene's single anchor point; an `Arc` orbits it, every other motion just has it in frame |

The matrix that gets generated is
**scene × motion template × camera × character × character animation**, and the
character dimension collapses to a single "no character" entry when character
handling is off. With `focus.mode = models` it gains one more axis —
**scene × camera × motion × focus object** — and the numbered folders of one motion
keep counting across it, with no per-object sub-folder.

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
Motion templates, Camera validation, Sequence output, **Camera region**, **Focus object**,
Local render, Actions, Status.

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

* **Project folder** — the folder a run writes into. Generation creates
  `blender_camera_<YYYYMMDD>/` inside it (re-running on the same day reuses it, which
  is what makes *Reuse existing sequences* work) holding:

  | Folder / file | What it is |
  |---|---|
  | `sequence/` | the sequence tree — what the renderer reads |
  | `scene/` | a copy of every source `.blend` the sequences are replayed onto (byte copy: textures are never downscaled or repacked) |
  | `video/` | render output |
  | `project.json`, `RENDER_README.md` | what the folder is, and the exact render command |

  The folder is **data only**. The renderer and the package live in the render image
  (`docker_blender/`, `/opt/mpp/blender_camera_motion_pipeline`); `render-all.sh` there
  uses the project's copy when one exists and the image's otherwise, so shipping a
  second copy per project only added ~2 MB and two versions to keep in sync. Render it
  with:

  ```bash
  render-all.sh <project>/sequence            # videos land in <project>/video
  render-all.sh <project>/sequence <output>   # or write them somewhere else
  ```

  That folder is the unit you zip to a render node. The panel shows the exact path it
  will create under the folder field, and *Open project folder* opens it.
* Generation runs **from the copy in `scene/`**, so what ships is what was validated.
  Each `sequence_config.json` records `source_blend` (absolute, the copy),
  `source_scene_rel` (`scene/<name>.blend`, relative to the project folder) and
  `source_blend_original` (where the scene came from). The renderer prefers the
  absolute path, falls back to the relative one resolved against the project root
  that `project.json` marks (or `--project-root`), and finally to `--path-map`.
* **Save validation report**, **Overwrite existing**, **Reuse existing sequences**,
  **Cameras** (`all`, names, or indices), and the render defaults recorded for the
  renderer.
* **Render defaults (recorded for the renderer)** — engine (a dropdown), samples,
  fps, video format, trajectory sampling and the **Sequence resolution**. All of
  them are written into every sequence's `sequence_config.json`, so a later headless
  render reproduces them.
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

### Focus object

Collapsed by default, and off until **Focus objects** is switched from `No focus
objects` to `One per model` (see [Focus objects](#focus-objects)).

* **Models (.blend)** — **Model file** + the picker adds one model; **Model folder**
  scans a folder for `.blend` models. The **Focus models (N)** list shows each row
  with an enable tick, the model name and (when set) the object it takes, with
  **move up / move down / remove / clear** under it. The selected row opens
  **Name**, **Object**, **Scale** and **Rotation**.
* **Anchor point (one per scene)** — `Auto (open spot)` / `From object` /
  `Numbers`. **Auto place anchor** creates or moves an empty — `MPP_FocusAnchor` by
  default — at the most open spot of the scene (with **Anchor clearance** metres of
  free space around it, and it says so when it finds less).
* **Arc shots** — **Keep the subject in frame**, **Required visibility** and
  **Skip shots that lose the subject**.

### Local render

Renders generated sequences to video **without leaving Blender**, and without
touching the file you have open: each sequence is rendered by a background
Blender process running the standalone renderer, so the panel uses exactly the
same code path a render farm does.

* **Sequences** — point **Sequence root** at a generated tree (the project's
  `sequence/` folder, a scene folder, a motion folder, or the whole tree) and press
  🔄 to list what is there. Or set **Sequence folder** to render one specific
  sequence. Folders already containing a video are listed as `Skipped`.
* The list shows `scene / motion / sequence` with a live per-row state
  (`pending` — `rendering` — `done` / `skipped` / `failed`), and failures show
  their reason in the row. (The panel also has a **Quick actions → Render video**
  button that fills this list in for you.)
* Sequences are animation-only: each one is rendered by replaying its stored camera
  animation onto the scene copy shipped in the project's `scene/` folder.
* **Save to** — where the videos go. Defaults to the project's `video/` folder when
  it can tell, otherwise `<parent of Sequence root>/render_output`. **Flat output**
  writes every sequence straight into that folder instead of the
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
· `Open project folder` · `View error report` · `Export configuration` /
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
    --output-root "D:\projects"

# One scene that Blender already has open
blender -b "D:\scenes\room001.blend" -P motion_pipeline_cli.py -- \
    --include-current --output-root "D:\projects"

# Select templates and narrow the frame range
blender -b -P motion_pipeline_cli.py -- \
    --scenes "D:\scenes\room001.blend" \
    --output-root "D:\projects" \
    --templates "E:\UE\...\camera_motion_templates.json" \
    --motion-filter "dolly_*" --motion-filter "pan_right_01_standard" \
    --frames 1:120 --fps 24

# Write the bare sequence tree instead of a project folder
blender -b -P motion_pipeline_cli.py -- \
    --scenes "D:\scenes\room001.blend" --sequence-root "D:\generated"

# Report what would happen, without writing anything
blender -b -P motion_pipeline_cli.py -- \
    --config batch.json --scene-dir "D:\scenes" --dry-run

# Check configuration and scenes only
blender -b -P motion_pipeline_cli.py -- --config batch.json --check-only

# Print the effective configuration
blender -b -P motion_pipeline_cli.py -- --print-config
```

`--output-root` is the **project folder** the dated project is created in (same
layout as the panel); `--sequence-root` keeps the historical behaviour of writing
the sequence tree straight into the folder you name. Exit codes: `0` success, `1` a
generation problem, `2` bad configuration/inputs.

`--no-sequence-blend` is still accepted and does nothing: sequences never write a
scene copy any more, so there is nothing to switch off.

Focus objects have a full set of flags too (`--focus-model`, `--focus-anchor*`,
`--focus-strict`, ...) — see [Focus objects](#focus-objects); `--dry-run` counts that
axis and lists the models.

### The 41-move batch skill

`skills/generate-41-shots/` packages the whole *folder of scenes → sequence tree* flow as a
skill that runs on any machine with Blender plus this repository, and it refuses to guess
the two numbers that are a judgement about the room:

```bash
python skills/generate-41-shots/inspect_scene.py --scenes /data/scenes --report /tmp/inspect.json
python skills/generate-41-shots/make_run_config.py --scenes /data/scenes --output /data/run_0924 \
    --items /data/item --region "-2.0,0.01,2.95:9.6,5.3,5.7" --anchor "-1.4,-1.2,0.02" --run
python skills/generate-41-shots/verify_run.py --run /data/run_0924 \
    --expect-cameras 3 --expect-motions 41 --expect-focus 2 --expect-sequences 246
```

`inspect_scene.py` measures the scene (interior bounds, floor, what each camera can
actually see, orbit-feasible spots for the subject); `make_run_config.py` writes the run
config and runs the CLI (`--region`/`--region-object` and `--anchor`/`--anchor-object` are
required, `--scenes` walks a folder recursively, `--items`/`--item` list the focus
models); `verify_run.py` reads the finished tree with no Blender and no add-on import and
reports the counts, the render settings, the region stages and how much of each arc shot
shows its subject. `SKILL.zh-CN.md` is the same document in Chinese.

---

## Headless rendering

A generated project folder is data only, so the renderer comes from the render image
(`docker_blender/`). One command does the whole folder:

```bash
render-all.sh <project>/sequence            # videos land in <project>/video
render-all.sh <project>/sequence <output>   # or write them somewhere else (use mounted storage)
```

It reads every `sequence_config.json` below the sequence root, uses the settings each
sequence recorded, prints a preflight + inventory, and writes
`render_all_<stamp>.log` and `render_all_<stamp>_summary.txt` next to the videos.
Exit codes: `0` all rendered, `1` something failed, `2` preflight/usage problem,
`3` nothing to render.

Without the image, use the renderer from this package (same file, resolves its own
package):

```bash
blender -b -noaudio --factory-startup -P render/render_sequences.py -- \
    --input-root "<project>/sequence" \
    --output-root "<project>/video" \
    --recursive
```

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

**The engine a sequence was generated with is a default, not a lock.** What
`sequence_config.json` records under `render` is used when the command line says
nothing:

| Precedence | Source |
|---|---|
| 1 (highest) | the command line / the panel's Local render: `--engine`, `--device`, `--samples`, `--denoise`, `--resolution-x/y`, `--resolution-percentage`, `--fps` |
| 2 | the sequence's own record: `engine` and `samples` always; the **resolution only** when the generator was told to stamp one (`resolution_explicit`) |
| 3 | the loaded scene's own settings |

So "generate with EEVEE, render on the farm with Cycles" is ordinary use:

```bash
render-all.sh <seq> <out> --engine CYCLES --device GPU --samples 64 --denoise --persistent-data
```

Measured on one sequence recorded as `BLENDER_EEVEE`: with no flags it rendered
`BLENDER_EEVEE` / 32 samples, and with `--engine CYCLES --device CPU --samples 4` it
rendered `CYCLES` / 4 samples — both wrote a video. **The engine and sample count
actually used are written into the `render` block of the `<sequence>.json` next to the
video**, so an overridden run is still self-describing. `render-all.sh` also retries a
failed sequence with another engine (EEVEE → CYCLES → WORKBENCH) unless `--no-fallback`.

Two traps: `--resolution-percentage` can land on an **odd** edge (25% of 180 is 45) and
H.264 needs even width and height, so Blender fails with `height not divisible by 2` —
give exact `--resolution-x/y` or pick a percentage that divides cleanly. And the render
node's image has to carry the current renderer: flags like `--engine` are parsed by the
`render_sequences.py` *inside* the image, so an image that has not been rebuilt needs the
RUNBOOK's instance toolkit plus `MPP_RENDERER`.

Workers only pay off when the bottleneck is per-process rather than per-GPU.
EEVEE renders one frame on the GPU, so several workers on one GPU compete for the
same VRAM: on a heavy scene (3.3 M polygons, 34 shadow-casting lights, 4.2 GB
resident) three workers made each frame ~10x slower instead of ~3x more
throughput. Measure before fanning out; `--workers 1` is the fastest option on a
single-GPU machine for scenes like that.

Exit codes: `0` success (including "everything was already rendered"),
`1` at least one sequence failed, `2` no sequences found, `3` unexpected error.

Supported flags: `--input`, `--input-root`, `--project-root`, `--output`,
`--output-root`, `--recursive`, `--scene-filter`, `--motion-filter`,
`--sequence-filter`, `--frame-start`, `--frame-end`, `--resolution-x`,
`--resolution-y`, `--resolution-percentage`, `--fps`, `--engine`, `--samples`,
`--device`, `--video-format`, `--codec`, `--crf`, `--video-bitrate`,
`--trajectory-mode`, `--trajectory-step`, `--overwrite`, `--skip-existing`,
`--no-skip-existing`, `--dry-run`, `--list`, `--workers`, `--keep-frames`,
`--frames-output`, `--log-level`, `--log-file`, `--config`, `--path-map`,
`--check-assets`, `--no-check-assets`, `--asset-report`, `--flat`, `--timeout`.

### Rendering on another machine (Linux render node)

The project folder is relocatable, so a render node needs Blender and nothing else
— no add-on installation, no path mapping:

```bash
# ship it
scp -r blender_camera_20260213 user@node:/data/proj/
# render it there (inside the render image; --output-root must be on mounted storage)
ssh node
render-all.sh /data/proj/blender_camera_20260213/sequence /data/out/video
```

Why this works with no arguments: each sequence finds its scene through
`source_scene_rel` (`scene/<name>.blend`) because `project.json` marks the project
root. The recorded `source_blend` is an absolute path of the authoring machine
(`E:/…`), so it cannot exist on the node — the relative form is what carries the shot
across. If the
folder was reorganised, say where it is with `--project-root <dir>`; `--path-map`
is then only needed for the assets *inside* those `.blend` files.

`--list` prints, per sequence, which scene file it will open and marks it
`scene missing` when it cannot find one, which makes it the right first command on
a new node:

```bash
blender -b -P render_sequences.py -- --input-root sequence --output-root video --list
```

### Making the project folder independent of the authoring machine

The sequences are fine — they carry data, not paths. The `.blend` copies in
`scene/` are the only thing that still refers to the machine that generated them
(textures, linked libraries, caches). Two ways to deal with it:

```bash
# A. bridge the paths on the render node (repeatable; applies to scenes and assets)
render-all.sh "<project>/sequence" --path-map "E:/UE/DataGenScenes=/mnt/data/DataGenScenes"

# B. pack everything into the copies once, then the folder is portable
blender -b -P /opt/mpp/blender_camera_motion_pipeline/render/pack_textures.py -- \
    --scene-root "<project>/scene"
```

`pack_textures.py` (shipped with the render image, and in this package) opens every
scene copy, packs its external files, saves it compressed in place and writes
`pack_report.json` next to the `scene/` folder listing what was packed and what could
not be found. Missing files are reported, never fatal. Useful flags: `--scene <file>`
(one file), `--dry-run` (report only), `--list`, `--no-compress`, `--report <path>`.

---

## Output layout

A run writes one **slim project folder**: the sequence tree and the scenes it needs —
data only, because the renderer comes from the render image — so the folder can be
zipped or uploaded as it is.

```text
D:\projects\                         <- the folder you pick (panel: Project folder)
└── blender_camera_20260213\         <- created by the run (reused on the same day)
    ├── project.json                 what this project is + the render command + scene map
    ├── RENDER_README.md             the same commands, for the render node
    ├── scene\                       a copy of every source .blend (textures untouched)
    │   └── room001.blend
    ├── video\                       render output
    └── sequence\                    the sequence tree (render input)
        ├── batch_config.json        effective configuration for this run
        ├── batch_report.json        per-scene and per-sequence outcome
        ├── manifest.json            roll-up of every sequence + every failure
        └── room001\
            └── dolly_in_01_standard\
                ├── manifest.json
                ├── sequence_000001\
                │   ├── sequence_config.json       what the generator decided
                │   ├── sequence_000001.json       camera trajectory + metadata + animation
                │   ├── sequence_000001_camera.txt camera trajectory, one row per frame
                │   ├── validation_report.json     per-frame metrics + search log
                │   └── generation_log.txt         step-by-step log for this sequence
                └── sequence_000002\
                    └── ...
```

`--sequence-root <dir>` (CLI) writes the same `sequence/` tree contents straight
into `<dir>`, without the project folder, for scripted runs that want exactly that.

Sequence numbering restarts at `sequence_000001` inside each motion folder, so a
motion folder is self-contained and re-running one motion never renumbers another.

**Generation packs the external files of every scene copy.** As soon as a copy lands
in `scene/`, `render/pack_textures.py` runs on it in a throwaway Blender process:
textures, fonts and movie clips are embedded into that `.blend`, and both generation
and later renders use the self-contained copy. The result lands in
`pack_report.json` in the project root (per scene: what was packed, what could not be
found, size change, seconds) and `project.json` carries an `asset_pack` summary; each
scene logs one `scene assets: <name> -- ... (N packed, M missing)` line.

Files that no longer exist on disk **cannot** be packed — that is reported as a problem
during generation (with the first few names) instead of surfacing hours into a render
as a silently missing texture. `scene/` only ever holds `.blend` files; the report goes
to the project root.

With focus objects on, `scene/` also holds **one copy per focus model**
(`<name>__<model>.blend`), and each of those copies carries **exactly one** subject:
a shot has one subject, so the file a render node opens says which.  The model stays
visible and its name is recorded in the scene as `mpp_focus_objects`, so a renderer that
predates the feature still films the right thing; the generator and the renderer still
switch visibility per sequence, which is what keeps a subject-less shot honest.  The run
writes `focus_report.json` beside `pack_report.json` in the project root (per copy: the
anchor, whether that model loaded, and its placement's world box).

**A sequence is animation-only.** It stores the camera animation it generated
instead of a copy of the scene — ~150 KB instead of hundreds of MB — and the
renderer replays it onto the scene shipped in `scene/`:

| | measured on the 265.9 MB reference scene |
|---|---|
| scene source | 265.9 MB (already compressed) |
| a scene copy per sequence (the mode that was removed) | 264.9 MB compressed / 610.6 MB uncompressed |
| **the animation payload, per sequence** | **≈ 150 KB** |
| packed textures carried by the project's single scene copy | 172.3 MB |
| geometry (2.29 M vertices / 3.3 M polygons) | the rest |
| camera + its generated action alone | ≈ 5.4 MB as a `.blend`, ≈ 28 KB as JSON |

So a 17-sequence project costs one scene copy (whatever your scene is) plus ~2.5 MB
of sequences, instead of 4.4 GB of duplicated scenes.

The payload records the values that were actually keyed — `location` in parent
space, `rotation_quaternion`, `scale` and the camera data's `lens`, per frame — plus
the interpolation and which constraints were muted for the bake, so replaying cannot
drift from what the generator validated. `tests/probe_animation_only_equivalence.py`
compares a replayed sequence against the trajectory the generator recorded: they
agree to `1e-4` in the W2C matrix, i.e. single-precision residue on a coordinate
431 m from the origin.

The trade-off is that the scene must travel with the sequences — which is exactly
what the project folder does, in `scene/`, with `source_scene_rel` recorded so the
folder keeps working after it is moved. Textures inside those `.blend`s still point
at the machine that generated them: either bridge them with `--path-map`, or run
`pack_textures.py` once to pack them into the copies. `--list` shows the storage
mode of every sequence, and `tests/probe_blend_size.py` prints the numbers above for
your own scene.

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
    "coordinate_system": "blender_world_to_camera",
    "rotation_representation": "3x3 rotation matrix rows r00..r22 (camera axes: +X right, +Y up, +Z back)",
    "view_axis": "the camera looks down local -Z, i.e. -(r20 r21 r22)",
    "inverse": "inv([R|t]) is the camera-to-world matrix (Blender matrix_world)",
    "opencv_equivalent": "flip rows 1 and 2 of the rotation and the translation for +Y down / +Z forward",
    "units": "blender_world_units (metres by default)",
    "distortion_slots": "d1..d5 are reserved and always 0"
  }
}
```

### Camera trajectory TXT

Header line and one row per frame, same column layout as the reference script
(which skips the `#` comment block):

```text
# sequence_id=sequence_000001
# scene=room001 motion=dolly_in_01_standard camera=Camera
# frames=0..80 fps=24
# coordinate_system=blender_world_to_camera (row0=+X right, row1=+Y up, row2=+Z back)
# rotation_representation=3x3 rotation matrix, rows r00..r22, column-vector convention
# view_axis=the camera looks down its local -Z, i.e. -(r20 r21 r22)
# inverse=inv([R|t]) is the camera-to-world matrix (Blender matrix_world)
# opencv_equivalent=flip rows 1 and 2 of R and t for +Y down / +Z forward
# translation=tx ty tz from W2C = [R^T | -R^T * camera_position]
# units=blender_world_units (metres by default); distortion d1..d5 reserved, always 0
frame focal_length d1 d2 d3 d4 d5 r00 r01 r02 tx r10 r11 r12 ty r20 r21 r22 tz
0 35 0 0 0 0 0 0.00000006 -1.00000000 ... -0.00000010
```

**The matrix is Blender's own**, so a viewer can invert it and draw the camera as
the scene has it (`tools/visualize_trajectory.py` does exactly that: `inv(w2c)` →
frustum along local `-Z`, up = local `+Y`, world `Z` up — no flags needed):

* rows are the camera's axes in world space — `row0 = +X` right, `row1 = +Y` up,
  `row2 = +Z` back, so the **view direction is `-row2`**;
* `det(R) = +1` (a proper rotation: `inv` gives a camera, not a mirror) and
  `inv([R|t])` equals Blender's `matrix_world` — both asserted by the tests;
* a point *in front of* the camera therefore has **negative** `z` in camera space
  (Blender looks down local `-Z`);
* for OpenCV's `+Y` down / `+Z` forward convention, flip rows 1 **and** 2 of the
  rotation and the translation (`diag(1, -1, -1)`), which keeps the determinant
  at `+1`. Flipping only row 1 — what this export used to do to imitate the
  reference — produces a reflection (`det = -1`) that no viewer can invert.

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
    "output_root": "D:/projects",
    "mode": "none",
    "scene_name_mode": "stem",
    "overwrite": false,
    "resume": true,
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
      "fps": 24.0,
      "rotation_order": "XYZ"
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
  "focus": {
    "mode": "off",
    "models": [{"id": "", "path": "E:/models/chair.blend", "label": "chair",
                "object_name": "", "scale": 1.0, "rotation": [0.0, 0.0, 0.0],
                "enabled": true}],
    "anchor_mode": "auto", "anchor_object": "MPP_FocusAnchor",
    "anchor_location": [0.0, 0.0, 0.0], "anchor_clearance": 0.5,
    "keep_visible": true, "visible_ratio": 0.95, "strict": false
  },
  "scenes": [{"path": "E:/scenes/room001.blend", "enabled": true}]
}
```

Keys are accepted in `snake_case`, `camelCase`, `kebab-case` or `UPPER_CASE`.
Unknown keys produce a warning instead of failing, so a config written for a
newer build still runs — including the removed `batch.save_sequence_blend` (no
sequence stores a scene copy any more) and the Unreal-era `motion.unit_scale`
mapping keys, all of which are now ignored rather than honoured.

---

## Motion templates

The generator reads any document shaped like the reference file — nothing about a
motion type is hard-coded in Python, so adding a motion means adding a JSON entry.

### Discovery order

1. `motion.template_path` (panel field / `--templates`) — an error if unreadable.
2. `motion.template_data` (inline array in the config).
3. `$MOTION_PIPELINE_TEMPLATES`.
4. The bundled `templates/camera_motion_templates.json`, then the known reference
   locations:
   `E:\UE\DataGenScenes\Plugins\MetaHumanScenePipeline\Templates`,
   `E:\UE\MetaHumanScenePipeline\Templates`, — a warning only, falling back to
   an embedded minimal set.

### Accepted shapes

```json
[{"id": "dolly_in_01_standard",
  "keys": [{"frame": 0,  "location": [0, 0, 0],    "rotation": [0, 0, 0], "focal": 35},
           {"frame": 40, "location": [0, 0, -1.5], "rotation": [0, 0, 0], "focal": 35},
           {"frame": 80, "location": [0, 0, -3.0], "rotation": [0, 0, 0], "focal": 35}]}]
```

`{"templates": [...]}`, `{"motion_templates": [...]}`, `{name: {...}}` and a
single template object are all accepted, as are the aliases `name`/`template` for
`id`, `keyframes`/`samples`/`frames` for `keys`, `position`/`pos` for `location`,
`angles`/`rot` for `rotation`, and `lens`/`focal_length` for `focal`. Extra
per-keyframe or per-template fields are preserved in `parameters`.

### Coordinate contract — Blender, with no conversion

Templates are authored in **Blender coordinates** and used **verbatim**: no axis
swap, no sign flip, no unit rescaling. The numbers are the camera's own local
transform, so a template reads like a pose you would set on the camera itself.

| Template | Meaning |
|---|---|
| `location` | offset in the camera's **own frame**, in **metres**: `+X` right, `+Y` up, `+Z` backwards — so **`-Z` is forward** (a 3 m push is `[0, 0, -3]`) |
| `rotation` | `[rx, ry, rz]` in **degrees** about the same local axes: `rx` tilts, `ry` turns left/right, `rz` rolls the frame. Composed in `motion.unit_scale.rotation_order` (default `XYZ`, Blender's own order) |
| `focal` | millimetres |

The offset is applied **in the camera's starting orientation**, so "0.5 m to my
right, 1.2 m up, 3 m forward" is `[0.5, 1.2, -3]` no matter how the camera is aimed
in the world.

Every axis is asserted by [`tests/probe_axes.py`](tests/probe_axes.py) and locked
down in `tests/test_motion_templates.py` — `dolly_in` really does push the camera
forward, `pan_right` really does turn it right, `pedestal_up` really does raise it,
`truck_right` really does strafe it right, and `roll` really does spin the frame
without changing the aim.

`motion.unit_scale` therefore carries only the timeline settings (`fps`,
`rotation_order`). The Unreal-era keys (`location_scale`, `location_forward/right/up`,
`yaw_axis`/`pitch_axis`/`roll_axis` and their signs) are gone; a config that still
sets them gets one warning naming the migration script, and they are ignored.

#### Migrating a template set written for Unreal

An Unreal set (``location`` in centimetres along `X` forward / `Y` right / `Z` up,
``rotation`` as `[roll, pitch, yaw]` with yaw about the **world** up axis) converts
once, offline:

```bash
python tests/migrate_unreal_templates.py --input templates.json --check     # report only
python tests/migrate_unreal_templates.py --input templates.json --in-place  # keeps a backup
python tests/migrate_unreal_templates.py --input templates.json --output blender.json
```

`location [forward, right, up]` cm → `[right, up, -forward]` m, and
`rotation [roll, pitch, yaw]` → local `[pitch, -yaw, -roll]`; ids, frames, focals
and every extra field are preserved. The script refuses to convert a document that
already looks Blender-native (offsets in metres are small) unless `--force` is
given, which is what makes it safe to run twice by accident.

The bundled `templates/camera_motion_templates.json`, the test set beside it and the
project's own reference document were migrated with it; the
`*.unreal_backup.json` copies left next to them are the pre-migration originals
(they are never discovered or loaded — discovery looks for exact file names).

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

### The 41 dataset moves

`templates/camera_motion_templates_41.json` is a second motion-template document:
the **41** moves of the dataset shot list — **17** single, **7** simultaneous,
**13** two-phase and **4** three-phase. It has the shape of any other document, so
the parser, the library, the panel and the probes treat it like the reference one,
and it is loaded the same way (`--templates`, or the panel's **Motion templates
path**).

| Convention | Value |
|---|---|
| Timeline | keys are **absolute frames** at 24 fps and the shot is 6 s, frames **0..144**; a two-phase move splits at frame 72 and a three-phase one at 48/96, and a phase with no component is a **hold** |
| Ids | `<kind>_<move>` — `single_arc_cw`, `sim_dolly_in__tilt_up`, `seq_pan_left__tilt_up`, `tri_dolly_in__static__dolly_out`. The kind prefix is load-bearing: the list has both a simultaneous and a sequential "pan left + tilt up" |
| Every entry | `dataset_key` (the original `S01_...` key), `cap_zh`, `camera_sentence`, `targets`, `tier`, `group`, `moves` (type, direction, phase) |

| Move | Nominal amplitude over the 6 s shot |
|---|---|
| Dolly In / Out | 3.0 m forward / backward |
| Truck Left / Right | 2.0 m |
| Crane Up / Down | 2.0 m |
| Pan Left / Right | 45 deg |
| Tilt Up / Down | 20 deg |
| Roll CW / CCW | 25 deg |
| Zoom In / Out | 35 → 70 mm / 35 → 18 mm |
| Arc CW / CCW | a 90 deg orbit around a subject 4 m ahead |

The keys are written in the same camera-local Blender coordinates as every other
template (`location` is `[right, up, back]`, so `-Z` is forward). An `Arc`'s keys sit
on the circle that keeps a subject 4 m ahead centred, with the yaw following the
orbit angle — the convention `atomic_motion_templates.json` uses too — and with a
**focus object** the generator re-bakes that orbit around the object itself: the circle
is centred on it, the camera is aimed at it on every frame, and the sweep and timing stay
the template's. The camera's own distance to the subject is tried first; when the room
cannot hold that circle (a 7 m orbit inside a 5 m bedroom drives the camera through a
wall) the same circle is replayed at the nearest distance that passes validation, stays
inside the camera box and keeps the subject in frame — the record says so
(`focus.radius_source`, `focus.radius_attempts`), and a camera-search candidate that
loses the subject is refused rather than accepted. The amplitudes are *nominal*: with the
region box on, translation is scaled to the scene, while angles and focal length never
are. Regenerate it with `python tests/make_41_templates.py` (`--verify-jsonl
<41template.jsonl>` cross-checks it against the source shot list row by row).

---

## Compound shots (spatio-temporal)

A compound shot is planned in **space and time**: the video is split into
**segments**, and each segment plays one or more *atomic* camera moves **at the
same moment**.  Two atoms may share a moment only when they drive different axes:

| channel | what it drives |
|---|---|
| `yaw` | rotation about the camera's own up axis (`ry`) — Pan, and the yaw half of Arc |
| `pitch` | rotation about the camera's own right axis (`rx`) — Tilt |
| `roll` | rotation about the camera's own view axis (`rz`) — Roll |
| `lateral` | translation along the camera's own right axis (`x`) — Truck, and the track half of Arc |
| `vertical` | translation along the camera's own up axis (`y`) — Pedestal |
| `depth` | translation along the camera's own view axis (`z`) — Dolly In/Out |
| `focal` | focal length (mm) — Zoom In/Out |

So `Pan right + Tilt down + Truck left` is a legal three-move segment, while
`Zoom In + Zoom Out` or `Pedestal up + Pedestal down` is refused (they fight over
one axis), and an `Arc` cannot be combined with a Pan or a Truck because it drives
both of those axes itself.

### The atomic vocabulary

`templates/atomic_motion_templates.json` holds 49 entries: Pan (left/right), Tilt
(up/down), Roll (clockwise/counterclockwise), Truck (left/right), Dolly In/Out,
Pedestal (up/down), Arc (clockwise/counterclockwise) and Zoom In/Out — **each in
slow / medium / fast** — plus `static`.  Regenerate it with
`python tests/make_atomic_templates.py`; every entry is an ordinary template whose
keys are a **one-second ramp**, so its delta *is* the rate per second:

| atom | slow | medium | fast |
|---|---|---|---|
| Pan | 8 deg/s | 18 deg/s | 40 deg/s |
| Tilt | 5 | 12 | 26 |
| Roll | 4 | 10 | 22 |
| Truck | 0.25 m/s | 0.6 m/s | 1.3 m/s |
| Dolly | 0.3 | 0.7 | 1.5 |
| Pedestal | 0.15 | 0.35 | 0.75 |
| Arc | 0.25 m/s lateral + `v/4 m` rad/s yaw | 0.6 | 1.3 |
| Zoom | 4 mm/s | 10 mm/s | 22 mm/s |

Because the atoms are rates, a segment's *length* changes how far a move travels,
not how fast it looks: "Pan left, medium" is 18 deg/s whether the segment lasts
0.5 s or 6 s.  Shot-specific families (``hitchcock``, ``fixed``, the numbered
variants) are deliberately absent — a compound builds those out of atoms instead.

### The settings (panel: *Sequence output* → *Compound shots*)

| Setting | Meaning |
|---|---|
| **Max moves at once** | 1-5 atoms may share a moment (`max_simultaneous`) |
| **Max segments** | how many segments a video may have (`max_segments`); every segment lasts at least **0.5 s**, which caps this for short videos |
| **Sequences per camera** | how many compound sequences **one camera** gets (`sequences_per_camera`).  Character/animation variants do **not** multiply it -- they are spread over the sequences, so four variants with a total of two still yields two compounds |
| **Random counts** | on: the two settings above are the *maxima* of per-sequence draws; off: every segment holds exactly that many moves and the video exactly that many segments.  Which atoms and speeds are drawn stays random either way, seeded through **Random seed** |
| **Video length** | `Fixed` (one length for every sequence) or `Random range` (`Min/Max seconds`, each sequence draws its own).  The frame range follows: `duration x fps` |
| **Compound output** | compounds together with the single-move shots, compounds only, or single moves only |
| **Atomic templates** | the vocabulary document; empty means the bundled one |

The CLI mirrors all of it: `--compound-simultaneous`, `--compound-segments`,
`--compound-random/--no-compound-random`, `--compound-templates`, `--duration`,
`--duration-mode`, `--duration-min/--duration-max`, `--compound-output`, plus
`--compound-seed`.  `--dry-run` prints the layout (segment cap, duration range and
an example plan) before anything is generated.

### What a compound produces

One sequence per camera, in the short ``combo/`` folder.  Alongside the usual
sidecar and trajectory it writes the **shot report**
(``<sequence>_motion_plan.json``), and the renderer re-emits the same file next to
the rendered video:

```json
[
  {"start_time": 0.0, "end_time": 1.0,
   "basic_movement": [{"type": "Tilt", "direction": "up", "speed": "fast"}]},
  {"start_time": 1.0, "end_time": 2.0,
   "basic_movement": [{"type": "Truck", "direction": "right", "speed": "slow"},
                      {"type": "Pedestal", "direction": "down", "speed": "medium"},
                      {"type": "Roll", "direction": "counterclockwise", "speed": "slow"}]}
]
```

Times are frame-exact multiples of `1/fps`, so the report and the video agree;
segments are contiguous and cover the whole video.  The full plan (segment list,
frame ranges, atom rates, seed) is also recorded in the sequence JSON's
``extra.motion_plan``, and `tests/probe_template_contract.py` re-flattens each plan
and compares it with the recorded poses frame by frame -- a plan-driven tree needs
no template document at all.


---

## Camera region

An optional box the camera has to stay inside. It has its own sidebar panel
(**Camera region**, below *Sequence output*); the Sequence output panel then repeats the
current box and verdict, because the box is what gets written into every
`sequence_config.json`.

The box is used by atomic and compound shots as a
**redraw** problem (a graded L0–L5 ladder: leave a good plan alone, redraw the segment
that left, redraw the plan, prefer slower same-family atoms, split long segments,
then fail honestly) and by fixed templates as a **fit** problem.

A fixed template is never redrawn — it is one shot, and its shape, timing and angles
*are* the shot. Its translation amplitude is multiplied by one factor so the whole
path stays inside the box; because `position(s) = first frame + s × offset` is affine
in `s`, the answer is exact and needs no search. Angles and focal length are never
scaled (they cannot leave the scene), and a shot that already fits is used unchanged.

| Recorded in `region` | Meaning |
|---|---|
| `stage: "fit"` | the fit ran: one uniform factor was computed and applied |
| `stage: "fit-failed"` | even the floor does not fit (the first frame is outside the box, which no scaling can fix) |
| `scale` | the factor used; `1.0` means the template was already inside the box and is untouched |
| `ok`, `frames`, `exit_frames`, `max_excess_m` | the post-fit verdict and the usual counts |

A re-centred focus orbit is the exception: its circle is centred on the subject, so
shrinking the offsets would slide the camera off it. It is **measured and reported**
instead (`stage: "focus-orbit"`, `scale: 1.0`) and `region.strict` decides whether the
sequence is kept — the same skip contract the ladder uses. See
[Focus objects](#focus-objects).

---

## Focus objects

Give a scene a **subject**: one or more `.blend` models are placed on the scene's single
**anchor point**, an `Arc` shot is made to **orbit** the object, and every other motion
is left exactly as it was, with the object simply in frame. It is off by default
(`focus.mode = off`) and stays out of the code paths when it is.

| Key | Default | Meaning |
|---|---|---|
| `mode` | `off` | `off` / `models` |
| `models` | `[]` | one entry per model: `path`, `label`, `object_name`, `scale`, `rotation`, `enabled` |
| `anchor_mode` | `auto` | `auto` (the most open spot of the scene) / `object` (`anchor_object`) / `numbers` (`anchor_location`) |
| `anchor_object` | `""` | read as `MPP_FocusAnchor` (the empty the panel button creates) |
| `anchor_location` | `[0,0,0]` | anchor point in world metres (`numbers`) |
| `anchor_clearance` | `0.5` | free space `auto` looks for around the anchor, in metres |
| `keep_visible` | `true` | check every sequence that the subject really stayed in frame |
| `visible_ratio` | `0.95` | share of the frames that counts as "in frame" |
| `strict` | `false` | do not write a sequence whose subject ended up out of frame |

**One anchor point per scene.** `auto` starts at the centre of the scene's bounding
box, drops to the floor under it and walks outwards over a small spiral until it finds
a spot with `anchor_clearance` metres of free space around it (26 ray directions); a
centre buried in a wall, a table or a train therefore still yields a usable anchor.
When nothing that open exists it takes the most open spot it did find and **says what
it measured**, and a scene it cannot measure at all falls back to the bounding-box
centre and says so. `object` takes the named object (falling back to the automatic
position, with a note, when it is not in the scene) and `numbers` takes the typed
coordinates. A model is placed by its footprint centre on the anchor and its
bounding-box base at the anchor's height, after `rotation`/`scale`.

**The object lives in the staged scene copy, and every object gets its own copy.** A
sequence ships the camera animation and the renderer replays it onto the copy in
`scene/`, so an object that is not inside that copy cannot be in the picture: the models
are placed while the copies are staged (`render/place_focus_objects.py`, a throwaway
Blender process next to `pack_textures.py`). N focus objects produce N extra copies
(`<scene>__<model>.blend`), each holding **exactly one** subject — one shot, one subject,
and the file the render node opens says which. The model stays visible and is registered
in the scene as `mpp_focus_objects`; the generator and the renderer still switch
visibility per sequence (`focus.apply_visibility`), which is what makes a subject-less
shot and a copy with several objects behave. Two consequences are deliberate: a renderer
that does not know about the feature still films the right thing, and the feature
therefore needs a project folder (the panel's and the CLI's normal path) — a bare
`--sequence-root` run has no copy to put an object into. The cost is disk: one scene copy
per subject (the reference bedroom's 113 MB became 120–138 MB per variant).

**The matrix gains one axis.** Sequences still land in
`<scene>/<motion>/sequence_NNNNNN` with **no per-object folder**: the numbering
restarts per motion folder and keeps counting across the objects, and every sequence
records which object it was generated for, in `sequence_config.json` → `focus` and in
the sidecar's `extra.focus`:

| `sequence_config.json` → `focus` | Contents |
|---|---|
| `object` / `label` / `model_path` | which subject this sequence is of (the configured id, its label, its file) |
| `objects` | the object names the renderer has to show (the only thing it switches on) |
| `anchor` / `center` | the anchor point and the subject's world box centre |
| `visibility` | `ok`, `visible_ratio`, `visible_frames` / `frames` |
| `orbit` | `radius_m`, `sweep_deg`, `direction` of the re-centred orbit |

With `focus.mode = off` the block is `{}`, and so is the block of a sequence that has
no subject (a single move, a compound): the renderer hides **every** focus object for
those, which is what keeps the previous sequence's subject out of the shot when a whole
tree renders in one Blender process.

**Only an `Arc` is re-centred.** The template's sweep and direction are kept verbatim
(`sweep_deg` and `direction` are read from it), but the circle is re-centred on the
object at the camera's **real horizontal distance** and the camera is aimed at it —
that aim is folded into the **base pose**, so the recorded trajectory, the validation
and the video all agree. The keys are resampled at 3 deg a step (a 3 mm chord error on
an 8 m orbit, against a measured 68 mm when only the template's own 15 deg keys were
used). A camera closer than 0.25 m to the subject horizontally cannot orbit it and is
told so; a motion that turns but is not an orbit (a pan, a hitchcock) is left completely
alone and the object is still in frame.

**Verification and honesty.** `keep_visible` (on by default) checks per sequence that
the object really stayed in frame — the box centre and at least one corner inside the
frustum counts as *visible*, all eight corners as *fully visible*, and both counts are
recorded — and `visible_ratio` (0.95 by default) is the threshold. A shot that misses
it is reported in full ("N of M frames visible, needing 95%"), and `focus.strict` skips
it instead of writing it, the same contract as `region.strict`. Frames where the camera
ends up inside the object also count against it.

Focus objects are scriptable too — everything the panel can say, the CLI can say:

```bash
blender -b --factory-startup -P motion_pipeline_cli.py -- \
  --scenes room.blend --output-root "D:\projects" \
  --templates templates/camera_motion_templates_41.json --motion-filter "single_arc_*" \
  --focus-model "D:\models\chair.blend::Chair::1.2" --focus-model "D:\models\lamp.blend" \
  --focus-anchor auto --focus-anchor-clearance 0.6 \
  --no-focus-keep-visible
```

| Flag | What it does |
|---|---|
| `--focus-mode off\|models` | the master switch; passing `--focus-model` implies `models` |
| `--focus-model PATH[::OBJECT[::SCALE]]` | repeatable; `OBJECT` takes one object out of the file, `SCALE` scales the model |
| `--focus-anchor auto\|object\|numbers` | where the anchor comes from |
| `--focus-anchor-object NAME` | the empty used by `object` (default `MPP_FocusAnchor`) |
| `--focus-anchor-location X,Y,Z` | the position used by `numbers` |
| `--focus-anchor-clearance M` | the free space `auto` looks for |
| `--focus-keep-visible` / `--no-focus-keep-visible` | the per-sequence visibility check (on by default) |
| `--focus-visible-ratio R` | the threshold for it (default `0.95`) |
| `--focus-strict` / `--no-focus-strict` | skip a sequence that loses the subject (default: record it) |

`--dry-run` counts the focus axis and lists the models (`N model(s), anchor …, keep_visible=…`).
A malformed `--focus-model` (no path, a scale that is not a number, too many `::`) is an
argparse usage error (exit 2), and `--focus-mode models` with no usable model — or focus
together with `--sequence-root`, which writes no scene copy to place the models in — is
refused *before* the run instead of quietly generating subject-less sequences. Rotation and
explicit ids stay in `--config` / the panel; every setting travels in the `BatchConfig`
(config file, panel export/import, remembered settings, `batch_config.json`).

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
# Everything (pure suites + Blender suites) — 294 cases
blender -b -P blender_camera_motion_pipeline/tests/run_blender_tests.py

# Pure suites only, no Blender required (177 cases)
python blender_camera_motion_pipeline/tests/run_blender_tests.py

# Individual suites (each one also runs on its own)
blender -b -P blender_camera_motion_pipeline/tests/test_blender_integration.py
blender -b -P blender_camera_motion_pipeline/tests/test_animation_api.py
blender -b -P blender_camera_motion_pipeline/tests/test_focus_objects.py
python blender_camera_motion_pipeline/tests/test_project_layout.py

# Probes are standalone too; tests/_boot.py makes the add-on importable whatever
# the package folder is called (it is registered under its historical name).

# Axis-convention sanity probe (prints what each motion family actually does)
python blender_camera_motion_pipeline/tests/probe_axes.py

# Background MP4 rendering smoke test
blender -b -P blender_camera_motion_pipeline/tests/smoke_render.py

# Full end-to-end acceptance run through the CLI + renderer
blender -b -P blender_camera_motion_pipeline/tests/verify_end_to_end.py

# Convert an Unreal-coordinate template set to Blender coordinates (once, offline)
python blender_camera_motion_pipeline/tests/migrate_unreal_templates.py \
    --input templates.json --check

# Bake algebra vs mathutils, for random rigs / parent offsets / poses
blender -b -P blender_camera_motion_pipeline/tests/probe_bake_math.py

# Does a batch anchor every sequence on the same camera pose?
blender -b -P blender_camera_motion_pipeline/tests/probe_anchor_drift.py -- "<scene.blend>" ["<templates.json>" [count]]

# Does the panel configuration survive a run that opens other scenes?
blender -b -P blender_camera_motion_pipeline/tests/probe_settings_persistence.py

# Are every icon identifier the UI passes to ``label(icon=...)`` valid here?
blender -b -P blender_camera_motion_pipeline/tests/probe_icons.py

# Does every sequence in a generated tree render the path it recorded?
blender -b -P blender_camera_motion_pipeline/tests/probe_all_sequences.py -- "<sequence root>"

# Does every sequence match the template numbers it was made from? (pure Python)
python blender_camera_motion_pipeline/tests/probe_template_contract.py -- \
    --sequence-root "<generated tree>" --templates "<templates.json>"

# What makes EEVEE slow on a given sequence (raytracing, lights, polygons)
blender -b -P blender_camera_motion_pipeline/tests/probe_render_cost.py -- "<sequence.blend>"

# Where the bytes in a sequence .blend come from, and what each storage choice costs
blender -b -P blender_camera_motion_pipeline/tests/probe_blend_size.py -- "<scene.blend>" "<sequence.blend>"

# A generated sequence replays to the path the generator recorded
blender -b -P blender_camera_motion_pipeline/tests/probe_animation_only_equivalence.py -- \
    "<sequence dir>"
```

`MP_KEEP_TEST_OUTPUT=1` keeps the integration artifacts for inspection;
`MP_KEEP_E2E=1` keeps the end-to-end tree; `MP_TEST_TRACEBACK=1` prints full
tracebacks.

Both entry points are worth running: `run_blender_tests.py` puts every suite in **one**
Blender process, while the suites also run one at a time as above. Cases rebuild the
fixtures they need (a model or scene in the temp folder that another throwaway Blender
process removed is recreated on demand), so both entry points should come back green.

### Results on this machine (Blender 5.2.2 LTS, Windows)

| Suite | Cases | Result |
|---|---|---|
| `test_path_utils` | 16 | pass |
| `test_config` | 19 | pass |
| `test_project_layout` | 14 | pass |
| `test_motion_templates` | 33 | pass |
| `test_motion_composite` | 23 | pass |
| `test_region` | 14 | pass |
| `test_region_planner` | 10 | pass |
| `test_region_wiring` | 9 | pass |
| `test_camera_validation` | 39 | pass |
| `test_animation_api` | 12 | pass |
| `test_addon_lifecycle` | 10 | pass |
| `test_render_workflow` | 18 | pass |
| `test_focus_objects` | 16 | pass (includes the CLI flags and the renderer's show/hide) |
| `test_blender_integration` | 61 | pass |
| **Total** | **294** (177 pure + 117 Blender) | 294 pass in the combined run |

The pure suites (177 cases) pass either way — that is the part of the suite that needs
no Blender, so it is also the part a machine without one can run. The Blender suites are
still being extended, so the case count moves.

`tests/static_check.py` scans every Python file for unused imports and leftover debug
markers and guards both READMEs against encoding damage — `U+FFFD`, and any CJK
character in this English document. One integration case drives every panel's `draw()`
against a stub layout — a panel that reads a property which no longer exists would
otherwise only fail when a user opens the sidebar.

End-to-end acceptance: **10/10 stages**

1. Build 3 fixture scenes.
2. CLI dry-run reports the matrix.
3. CLI generates the sequences with the real 80-template document.
4. The project folder is data only and self-describing (`sequence/` + `scene/` + `video/`).
5. Generated artifacts match the documented layout.
6. The package's renderer (in production: the image's) produces the videos from it.
7. Every video has its JSON + trajectory TXT beside it.
8. `ffprobe` confirms H.264 and 81 frames per video.
9. Re-running the renderer skips finished sequences and exits 0.
10. `--list` enumerates sequences and their state.

The add-on was also installed into a real Blender add-ons folder and verified to
enable, expose all 26 operators and 10 panels, discover and pre-fill the template
document (80 templates), and disable cleanly.

Coverage includes the brief's edge cases: missing scene path, scene without a
camera, multiple cameras, missing/malformed template JSON, geometry blocking the
lens, every candidate failing, character assets missing, unreadable output
targets, pre-existing sequences, background execution, and missing external
assets.

---

## Architecture

```text
blender_camera_motion_pipeline/
├── __init__.py add-on entry (bl_info + register/unregister)
├── _bootstrap.py make the package importable whatever the folder is called
├── registration.py registration order, reload, preference defaults
├── properties.py scene PropertyGroup  <->  BatchConfig
├── operators.py the panel operators (+ the generation/render timers)
├── panels.py sidebar panels and the two UILists
├── preferences.py add-on preferences, durable task status
├── motion_pipeline_cli.py headless generation CLI
├── templates/             the motion template documents (data, not code)
│   ├── camera_motion_templates.json       the 80-template reference set
│   ├── camera_motion_templates_41.json    the 41 dataset moves (see Motion templates)
│   ├── atomic_motion_templates.json       the 49-entry atomic vocabulary
│   ├── camera_motion_templates_light.json 17-template subset
│   ├── camera_motion_templates_test.json  one-template smoke set
│   └── *.unreal_backup.json               pre-migration Unreal originals
├── config/
│   ├── models.py typed config dataclasses, lenient parsing, validation
│   ├── defaults.py defaults + template discovery (templates/ -> legacy config/)
│   ├── panel_state.py the remembered panel configuration
│   ├── schema.json        JSON schema
│   └── example_config.json
├── core/                  orchestration (needs bpy)
│   ├── scene_loader.py discovery, de-duplication, safe loading
│   ├── blender_context.py camera snapshots, geometry harvest, restore
│   ├── sequence_generator.py one sequence: validate -> search -> bake -> write
│   ├── batch_runner.py scenes x motions x cameras x characters
│   ├── focus.py the focus subject: anchor, orbit retarget, visibility, registry
│   ├── region.py the camera box: metrics and the exact translation fit
│   ├── project.py the slim (data-only) project folder a run writes
│   ├── camera_animation.py the animation payload + how it is replayed
│   ├── sequence_manager.py read-only view of the output tree
│   └── ui_task.py incremental, cancellable timer state machine
├── camera/                camera maths and checks
│   ├── motion_templates.py parser, interpolation, keyframe generation
│   ├── scene_context.py     MeshSnapshot / CameraSnapshot / ray casters
│   ├── camera_validator.py clipping, occlusion, framing, jumps, scoring
│   ├── camera_search.py candidate generation, aim, spherical search
│   ├── region_planner.py    the L0-L5 re-draw ladder for plans
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
│   ├── pack_textures.py    pack a scene copy's external files into it
│   ├── place_focus_objects.py stage the focus models into a scene copy
│   ├── render_runner.py panel render driver (child Blender processes)
│   └── metadata_exporter.py JSON + trajectory writers for the renderer
├── utils/
│   ├── logging_utils.py console + file + UI-callback logging
│   ├── task_control.py cancellation and progress
│   ├── animation.py version-agnostic Action/F-Curve access
│   └── version.py version stamping
└── tests/                 harness + suites + probes (see Testing)
    └── _boot.py           registers the package name for standalone runs
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
3. **Scenes are copied, not referenced.** A run copies every queued `.blend` into
   the project's `scene/` folder and generates from the copy, so the project is
   self-contained — which costs one scene's worth of disk per project (a 265 MB
   scene costs 265 MB, and re-running the same day reuses the copy instead of
   re-copying it). Textures inside those copies still point at the machine that
   generated them until you run `pack_textures.py` or map the paths with
   `--path-map`.
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
12. **Focus objects need a staged project copy — one per subject.** The models are baked
    into copies in `scene/` (`<scene>__<model>.blend`, one subject each, visible and
    registered as `mpp_focus_objects`) because a sequence ships the camera animation
    only, so a bare `--sequence-root` run has nowhere to put an object and the axis is
    empty there. A run pays one scene copy per subject in disk. The object is placed on
    the anchor by its **footprint centre** and its bounding-box base, and only the
    objects the model file actually brings in are registered — a model that
    contributes nothing is reported and skipped rather than becoming a folder of
    subject-less sequences.
13. **An arc retarget applies to fixed templates, not to compound plans.** A
    compound or atomic plan keeps its own arc (which assumes the nominal subject
    distance), and `Arc` is recognised the way the document spells it (`type` in the
    template's parameters, or an id starting with `arc`). A re-centred orbit that
    leaves the region box is **reported** (`stage: "focus-orbit"`) rather than
    shrunk — its radius is the camera's distance to the subject, so scaling the
    amplitude would slide the camera off it — and `region.strict` decides whether
    that sequence is kept.

---

## License / attribution

Motion template semantics are derived from the Unreal reference project
(`E:\UE\MetaHumanScenePipeline`, `E:\VSCode\CameraCtrl\movie_render.py`), which was
read for analysis only and never modified.
