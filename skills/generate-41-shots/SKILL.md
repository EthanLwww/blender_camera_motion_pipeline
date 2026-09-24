# Skill: generate the 41-move camera dataset

Turns a folder of interior scenes into a sequence tree of **41 camera moves**
(`templates/camera_motion_templates_41.json`) for every camera and every focus
object, ready for the render node.  Everything here ships inside the add-on package,
so a machine only needs Blender plus this repository.

```
skills/generate-41-shots/
├── inspect_scene.py     step 1: measure a scene (Blender)      -- reports, decides nothing
├── make_run_config.py   step 2: build run_config.json, optionally run it (plain Python)
├── verify_run.py        step 3: check the sequence tree        -- no Blender, no add-on
└── SKILL.md / SKILL.zh-CN.md
```

## Inputs

| Input | Flag | Notes |
|---|---|---|
| scenes | `--scenes PATH` | a `.blend` **or a folder** — a folder is walked recursively, so one run covers many scenes |
| output | `--output DIR` | the project folder (`blender_camera_<date>/{sequence,scene,video}`) is created inside it |
| focus objects | `--items DIR` | a folder of `.blend` models; `--item "PATH[::LABEL[::SCALE]]"` names one explicitly |
| camera region | `--region "cx,cy,cz:sx,sy,sz"` or `--region-object NAME` | **required** |
| focus anchor | `--anchor "x,y,z"` or `--anchor-object NAME` | **required** |

The box and the anchor are required on purpose.  `motion_pipeline_cli.py` can also run
from a config alone, and the plugin has an automatic box (`region.mode=auto`) and an
automatic anchor (`--focus-anchor auto`); this skill does not use either, because both
quietly decide something that is a judgement about the room — see *Rules* below.

## Step 1 — measure the scene

```bash
blender -b -noaudio --factory-startup -P skills/generate-41-shots/inspect_scene.py -- \
    --scenes /data/scenes --report /tmp/inspect.json
```

The report (JSON, also printed) carries, per scene:

* `interior` — the bounds of the room, with environment objects (domes, backdrops,
  anything an order of magnitude bigger than the room) listed in `excluded_objects`
  so you can see what was left out;
* `floor_z` and `floor_note` — what a downward ray under the middle of the room lands on;
* `cameras[]` — position, lens, and `first_hits`: the first surfaces along each camera's
  view axis.  A camera whose first hit is 0.5 m away is pointed at a wall; one that sees
  metres of room is usable;
* `anchor_candidates[]` — open floor positions ranked by **orbit feasibility**: a
  re-centred `Arc` starts from *the camera's own distance to the subject*, and the
  generator only replays the circle closer or further when that distance does not
  survive the room.  A candidate with a clean `orbit_bad_frames` for every camera is
  therefore the one where the authored arcs stay authored.  Each candidate lists
  `orbit_bad_frames` per camera, the orbit `radius_m` and the surface it stands on;
* `region_suggestion` — the measured interior minus 5 cm, as a starting point.

**Choosing the box.** Take `region_suggestion`, then check it against the room rather
than the numbers: it should contain every camera's start *with the 0.25 m margin applied*
(`center ± (size/2 − margin)`), and it should not include the neighbouring flat, the
outdoor slab or the environment dome.  If a camera sits in an adjoining space that is
open to this one, either extend the box to cover both or leave that camera out of the
run — a camera outside the box is reported as `stage: "start-outside"` and its path is
not fitted at all.

**Choosing the anchor.** Take the best-ranked candidate whose `surface` is the floor you
want the subject to stand on (a rug is fine; a bed is not), put its `z` at that surface's
height, and sanity-check the distances to the nearest wall and furniture against the
models' footprints.  `--items` models are placed with their footprint centred on the
anchor and their base at the anchor's `z`.

## Step 2 — build the config and generate

```bash
python skills/generate-41-shots/make_run_config.py \
    --scenes /data/scenes \
    --output /data/sequences/run_0924 \
    --items /data/item \
    --region "-2.0,0.01,2.95:9.6,5.3,5.7" \
    --anchor "-1.4,-1.2,0.02" \
    --fps 24 --resolution 1280x720 --engine BLENDER_EEVEE \
    --run
```

Without `--run` it only writes `<output>/run_config.json` and prints the exact
generation command, which is the reproducible form:

```bash
blender -b -noaudio --factory-startup -P motion_pipeline_cli.py -- \
    --config /data/sequences/run_0924/run_config.json \
    --report /data/sequences/run_0924/batch_report.json
```

Useful extras: `--motion-filter "single_arc_*"` (glob) to generate a subset,
`--dry-run` to resolve the matrix first, `--no-validation`/`--no-search` to trade
honesty for speed, `--region-strict`/`--focus-strict` to skip a shot instead of writing
one that leaves the box or loses the subject.

`make_run_config.py` records the resolution it was given (`resolution_explicit`), so the
sequences render at that size without the render node repeating it, and it sets
`validation.obstruction_distance = 0.3`: the 0.5 m default is tight for an interior, and
an orbit that swings past a wall then trips the check frame after frame.

## Step 3 — check what came out

```bash
python skills/generate-41-shots/verify_run.py \
    --run /data/sequences/run_0924 \
    --expect-cameras 3 --expect-motions 41 --expect-focus 2 --expect-sequences 246
```

It reads only the JSON the generator wrote, so it runs anywhere the output folder is
(read a run on the render node, or after copying it around).  It prints the batch totals,
the per-motion / per-camera / per-focus-object counts, the render settings, the region
stages, and how much of the arc shots actually shows its subject; it exits non-zero on a
structural problem (a failed sequence, a missing motion folder, a count that does not
match `--expect-*`, an odd video dimension, a mixed resolution).  `--strict` also fails on
the softer findings — a shot whose camera left the box, a subject that was not in frame.

Two numbers worth reading every time:

* **`arcs`** — an `Arc` is *about* its subject, so its subject should be in frame for
  ~100% of the frames.  Anything lower means the circle was squeezed (see below).
* **`region`** — `inside` / `fit` are healthy; `fit-failed` means the template could not be
  made to fit even at 5% amplitude, and `start-outside` means that camera starts outside
  the box (fix the box or drop the camera).

## What comes out

```
<output>/blender_camera_<date>/
├── project.json, RENDER_README.md, batch_report.json, pack_report.json, focus_report.json
├── scene/<scene>.blend              the staged copy of the scene
├── scene/<scene>__<model>.blend     one copy per focus object: exactly one subject each
├── sequence/<scene>/<motion>/sequence_NNNNNN/
│   ├── sequence_config.json         what the renderer reads (camera, frames, region, focus)
│   ├── sequence_NNNNNN.json         trajectory + metadata + the animation payload
│   ├── sequence_NNNNNN_camera.txt   per-frame world-to-camera matrix
│   └── validation_report.json, generation_log.txt   (unless --drop-reports)
└── video/                           the render node writes here
```

One folder per motion, **no per-object sub-folder**: the focus object of a sequence is in
its `sequence_config.json` (`focus.objects`), and the numbering restarts per motion
folder and counts across cameras and objects.

### How an Arc treats its subject

`Arc` is the one family that *uses* the focus object; every other move only has it standing
in the scene.  An arc is re-authored around the subject: the circle is centred on it, the
camera is aimed at it for every frame, and the sweep and timing come from the template
unchanged.  Its block in `sequence_config.json` says what happened:

```json
"focus": {"object": "chair", "objects": ["Wooden Office Chair"],
          "anchor": [-1.4, -1.2, 0.02], "center": [-1.4, -1.2, 0.46],
          "orbit": {"radius_m": 0.71, "radius_natural_m": 3.55, "radius_source": "adapted",
                    "sweep_deg": -90.0, "direction": "clockwise",
                    "radius_attempts": [{"radius_m": 3.55, "passed": false,
                                         "inside_box": false, "reasons": ["camera_clipping"]},
                                        {"radius_m": 0.71, "passed": true, "inside_box": true,
                                         "reasons": []}]},
          "visibility": {"ok": true, "visible_frames": 145, "frames": 145,
                         "visible_ratio": 1.0}}
```

`radius_natural_m` is the camera's own distance to the subject — the shot the author
framed, and the one tried first.  A room can be too small or too cluttered for it (a 7 m
circle inside a 5 m bedroom drives the camera through a wall), so the same circle is
replayed at the nearest distance the scene accepts: each candidate has to pass validation,
stay inside the camera box, and keep the subject in frame.  `radius_source: "adapted"` plus
`radius_attempts` is the record of that (only written when more than one distance was
tried); the run log says it out loud
(`the authored 3.55 m distance does not survive the scene; using 0.71 m instead`).  The
camera search is not allowed to "fix" an arc by turning away from its subject: a candidate
that loses the subject is refused and logged as `focus_object_lost`.

Render it anywhere (see the main README): `render-all.sh <project>/sequence`, and
`--engine`/`--device`/`--samples` on that command override what the sequences recorded.

## Rules that keep the dataset honest

1. **No automatic box, no automatic anchor.**  `region.mode=auto` fits everything,
   environment dome included; `--focus-anchor auto` picks a spot from a spiral search
   that knows nothing about where the cameras look.  Measure, then decide.
2. **A focus object has to be inside the staged scene copy** (sequences ship the camera
   animation only, and the renderer replays it onto that copy), so this skill always
   runs through a project folder — never `--sequence-root`.
3. **`--resolution-percentage` can land on an odd edge** (25% of 180 is 45) and H.264
   needs even width *and* height; give exact `--resolution WxH` instead.
4. **The engine recorded in a sequence is a default.**  The render node overrides it with
   `--engine`; what was actually used is written into the rendered `<sequence>.json`.
5. **The render node's image has to carry this renderer.**  `--engine` and friends are
   parsed by the `render_sequences.py` inside the image: rebuild it, or use the
   RUNBOOK's instance toolkit plus `MPP_RENDERER`.
6. **Sequences are generated from a copy of each scene**, so a run never touches the
   scene files; the copy is staged once per scene and reused across runs the same day.

## Cost

Generation is analytic (no rendering): roughly **1.5 s per sequence** on an interior
scene with validation and the camera search enabled — 3 cameras × 41 moves × 2 focus
objects = 246 sequences in about 7 minutes.  Disk is ~200 KB per sequence plus the
scene copies (113 MB for the reference bedroom, with its packed textures).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `the sequence has no focus object` / no `focus` axis | the models were not placed: no project folder, or a model file missing | run with `--output` (a project folder), check `focus_report.json` |
| `stage: "start-outside"` in a sequence's region block | that camera starts outside the box | extend the box to cover it, or drop that camera from the run |
| an `Arc` whose question marks `radius_source: "adapted"` | the authored distance does not fit the room; the circle was replayed closer or further | check `visibility.visible_ratio` is ~1.0; move the anchor if the adapted distance looks wrong |
| an `Arc` that failed (`no distance around the subject passes validation`) | every radius in the ladder clips geometry or leaves the box | move the anchor (or widen the box) and regenerate; the reason per radius is in the run log |
| `validation_failed: camera_obstructed` on many shots | `validation.obstruction_distance` is too tight for the room | this skill writes 0.3 m; a hand-written config may still carry the 0.5 m default |
| every video is the scene's own size, not the one asked for | the config predates `resolution_explicit` | add `"resolution_explicit": true` to the `render` section, or pass `--resolution` |
| `no scenes to process` | `--scenes`/`--scene-dir` were not given and the config lists none | pass `--scenes` (the skill's config carries them; a hand-written config may not) |
| `height not divisible by 2` | odd resolution after a percentage | use `--resolution WxH` |
