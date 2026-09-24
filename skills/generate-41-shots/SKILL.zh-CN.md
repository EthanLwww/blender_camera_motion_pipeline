# Skill：生成 41 运镜视频序列数据集

把一个（或一批）室内场景，按 **41 个运镜**（`templates/camera_motion_templates_41.json`）
对每台相机 × 每个焦点物体生成一套序列树，交给渲染节点出片。整套东西都在插件包内，
换一台机器只要有 Blender + 这份仓库就能跑。

```
skills/generate-41-shots/
├── inspect_scene.py     第一步：勘察场景（Blender 里跑）—— 只报告，不做决定
├── make_run_config.py   第二步：生成 run_config.json，可直接接着跑（纯 Python）
├── verify_run.py        第三步：体检产出的序列树 —— 不需要 Blender，也不导入插件
└── SKILL.md / SKILL.zh-CN.md
```

## 输入（三个路径 + 两个必须自己给的数）

| 输入 | 参数 | 说明 |
|---|---|---|
| 场景 | `--scenes PATH` | 一个 `.blend` **或一个文件夹**；文件夹会递归遍历，一次覆盖多个场景 |
| 输出 | `--output DIR` | 项目文件夹 `blender_camera_<日期>/{sequence,scene,video}` 建在它里面 |
| 焦点物体 | `--items DIR` | 放模型 `.blend` 的文件夹；也可用 `--item "PATH[::LABEL[::SCALE]]"` 逐个指定 |
| 相机限制盒 | `--region "cx,cy,cz:sx,sy,sz"` 或 `--region-object NAME` | **必填** |
| 焦点位点 | `--anchor "x,y,z"` 或 `--anchor-object NAME` | **必填** |

盒子和位点故意做成必填。插件本身有自动盒子（`region.mode=auto`）和自动位点
（`--focus-anchor auto`），这个 skill **都不用**：它们会替你做两个本该公司判断的决定
（见下面"规则"）。

## 第一步：勘察场景

```bash
blender -b -noaudio --factory-startup -P skills/generate-41-shots/inspect_scene.py -- \
    --scenes /data/scenes --report /tmp/inspect.json
```

报告（JSON，同时打印到 stdout）里每个场景含：

- `interior`：房间的实测边界；环境球/背景板/比房间大一个数量级的东西会被列进
  `excluded_objects`（让你看见排除了什么，而不是替你默认排除）；
- `floor_z` / `floor_note`：房间中央向下打射线落在什么面、多高；
- `cameras[]`：位置、焦距，以及 `first_hits`——沿每台相机视线方向的头几个命中面。
  **第一个命中只有 0.5 m 的相机是在拍墙**，能看到好几米房间的才可用；
- `anchor_candidates[]`：按**环绕可行性**排序的空地位点。重定心后的 `Arc` 起点取
  *相机到主体的真实距离*，只有这个距离在房间里活不下来时生成器才会把同一个圆重放得更近或更远；
  因此"每台相机 `orbit_bad_frames` 都为 0"的候选，就是原作者的环绕能被原样保留的位点。
  每个候选给出各相机的 `orbit_bad_frames`、轨道半径 `radius_m`、以及它站在什么面上；
- `region_suggestion`：实测室内边界内缩 5 cm，作为起手值。

**怎么定盒子**：拿 `region_suggestion` 当起点，然后对着房间核，而不是对着数字核——
它要包含每台相机的起点**并留出 0.25 m 余量**（可用范围是 `center ± (size/2 − margin)`），
并且不要把隔壁、室外平台、环境球包进来。如果某台相机在与之连通的另一间屋里，要么把盒子
扩到两间都覆盖，要么这次不生成那台相机——盒子外的相机会被记成 `stage: "start-outside"`，
它的路径**完全不做幅度适配**。

**怎么定位点**：取排序最前、且 `surface` 是你希望主体站的地面（地毯可以，床不行）的那个候选，
把 `z` 设成该面的高度，再用模型的占地尺寸核对它与最近墙面/家具的距离。`--items` 里的模型
会以**占地中心对齐位点、底面贴位点 z** 的方式摆放。

## 第二步：生成配置并开跑

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

不加 `--run` 时它只写出 `<output>/run_config.json` 并打印生成命令；那份配置就是可复现形式：

```bash
blender -b -noaudio --factory-startup -P motion_pipeline_cli.py -- \
    --config /data/sequences/run_0924/run_config.json \
    --report /data/sequences/run_0924/batch_report.json
```

常用附加项：`--motion-filter "single_arc_*"`（支持 glob）只跑子集；`--dry-run` 先解矩阵不生成；
`--no-validation` / `--no-search` 用诚实度换速度；`--region-strict` / `--focus-strict`
让越界或丢主体的序列**跳过不写**，而不是照写。

`make_run_config.py` 会把你给的分辨率一起记进序列（`resolution_explicit`），渲染节点不用再重复一遍；
它同时把 `validation.obstruction_distance` 设成 0.3：0.5 的默认值对室内太紧，
环绕时从墙边扫过会逐帧被判"挡镜头"。

## 第三步：体检产出

```bash
python skills/generate-41-shots/verify_run.py \
    --run /data/sequences/run_0924 \
    --expect-cameras 3 --expect-motions 41 --expect-focus 2 --expect-sequences 246
```

它只读生成器写下的 JSON，因此在任何拿得到这份产出的机器上都能跑（渲染节点上、或把产出拷来拷去之后）。
它打印批次总数、按运镜/相机/焦点物体的计数、渲染设置、region 阶段分布，以及各 arc 镜头里
主体到底出现了多少帧；结构性问题（有失败序列、运镜文件夹缺失、数量与 `--expect-*` 不符、
视频边长是奇数、分辨率不统一）会返回非 0 退出码。加 `--strict` 时，"相机出盒""主体不在画面里"
这类软问题也算失败。

每次都值得看两个数字：

* **`arcs`** —— `Arc` 是**拍主体**的运镜，主体应该几乎 100% 的帧都在画面里；明显偏低说明圆被压缩过
  （见下）。
* **`region`** —— `inside` / `fit` 是健康的；`fit-failed` 表示模板连 5% 幅度都放不进盒子，
  `start-outside` 表示那台相机起点就在盒子外（改盒子，或这次不生成它）。

## 产出

```
<output>/blender_camera_<日期>/
├── project.json、RENDER_README.md、batch_report.json、pack_report.json、focus_report.json
├── scene/<场景>.blend             场景的暂存副本
├── scene/<场景>__<模型>.blend     每个焦点物体一份副本：每份里只有一个主体
├── sequence/<场景>/<运镜>/sequence_NNNNNN/
│   ├── sequence_config.json      渲染节点读的就是它（相机、帧范围、region、focus）
│   ├── sequence_NNNNNN.json      轨迹 + 元数据 + 相机动画载荷
│   ├── sequence_NNNNNN_camera.txt 逐帧 world-to-camera 矩阵
│   └── validation_report.json、generation_log.txt（`--drop-reports` 可不要）
└── video/                        渲染节点写这里
```

**每个运镜一个文件夹，不按物体分子文件夹**：某条序列用了哪个焦点物体写在它的
`sequence_config.json` 的 `focus.objects` 里；编号在每个运镜文件夹内重新开始，并跨相机、
跨物体连续。

### `Arc` 怎么对待主体

`Arc` 是唯一**真的用到**焦点物体的运镜家族；其他运镜里物体只是站在场景里。arc 会围绕主体
重新编排：圆心落在主体上、每一帧都对着主体，扫过角度与节奏完全照模板不变。它在
`sequence_config.json` 里的记录说明了发生了什么：

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

`radius_natural_m` 是相机到主体的原始距离，也就是作者当初构图的机位，**永远先试它**。
房间可能太小或太挤容不下它（5 m 深的卧室里套 7 m 的圆，相机会穿墙），于是同一个圆会
按场景能接受的距离重放：每个候选半径都要通过几何校验、留在相机限制盒内、并且主体在画面里。
`radius_source: "adapted"` 与 `radius_attempts` 就是这件事的记录（只在试过不止一个距离时才写），
运行日志里也会明说（`the authored 3.55 m distance does not survive the scene; using 0.71 m
instead`）。相机搜索**不允许**靠"转开不看主体"来修好一条 arc：丢掉主体的候选会被拒绝，
并记成 `focus_object_lost`。

渲染（见主 README）：`render-all.sh <项目>/sequence`；这条命令上的 `--engine`/`--device`/
`--samples` 会覆盖序列里记录的引擎。

## 保持数据集诚实的几条规则

1. **不要自动盒子、不要自动位点。** `region.mode=auto` 会把环境球一起框进来；
   `--focus-anchor auto` 的螺旋搜索完全不知道相机在看哪。先量，再定。
2. **焦点物体必须在暂存场景副本里**（序列只带相机动画，渲染节点把它回放到副本上），
   所以这个 skill 始终走项目文件夹，**不要用 `--sequence-root`**。
3. **`--resolution-percentage` 可能算出奇数边长**（180 的 25% = 45），而 H.264 要求宽高都是偶数；
   用 `--resolution WxH` 给准数。
4. **序列里记录的引擎只是默认值**，渲染节点用 `--engine` 就能换；实际用了什么会写进出片旁的
   `<序列>.json`。
5. **渲染节点的镜像必须带当前渲染器**：`--engine` 这些参数由镜像里的 `render_sequences.py` 解析，
   镜像没重建就用 RUNBOOK 的实例工具包 + `MPP_RENDERER`。
6. **序列是从场景副本生成的**，运行不会动你的场景文件；副本每个场景只暂存一次，当天重跑复用。

## 开销

生成是解析式的（不渲染）：室内场景开着校验与相机搜索时**约 1.5 秒/条**——
3 相机 × 41 运镜 × 2 焦点物体 = 246 条约 7 分钟。磁盘约 200 KB/条，外加场景副本
（参考卧室 113 MB，含打包贴图）。

## 故障速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 没有 focus 轴 / 提示序列没有主体 | 模型没被摆进去：没有项目文件夹，或模型文件不存在 | 用 `--output` 走项目文件夹；看 `focus_report.json` |
| 某条序列 region 块里是 `stage: "start-outside"` | 那台相机起点在盒子外 | 把盒子扩到覆盖它，或这次不生成那台相机 |
| 某条 `Arc` 记着 `radius_source: "adapted"` | 原始距离放不进房间，圆被重放成更近或更远 | 看 `visibility.visible_ratio` 是否接近 1.0；不合适就换位点 |
| 某条 `Arc` 失败（`no distance around the subject passes validation`） | 阶梯里每个半径都撞几何或出盒子 | 换位点（或放宽盒子）后重跑；每个半径的原因在运行日志里 |
| 大量 `validation_failed: camera_obstructed` | `validation.obstruction_distance` 对房间太紧 | 本 skill 写的是 0.3 m；手写配置可能还是 0.5 默认值 |
| 成片尺寸是场景自己的、不是要求的那档 | 配置早于 `resolution_explicit` | 在 `render` 段加 `"resolution_explicit": true`，或传 `--resolution` |
| `no scenes to process` | 没给 `--scenes`/`--scene-dir`，配置里也没有场景 | 传 `--scenes`（本 skill 生成的配置里带着场景；手写的配置可能没有） |
| `height not divisible by 2` | 百分比算出了奇数分辨率 | 用 `--resolution WxH` |
