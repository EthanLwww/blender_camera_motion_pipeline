# Motion Pipeline —— Blender 插件 + 无头渲染器（中文说明）

批量加载 `.blend` 场景，按 JSON 模板生成相机运动序列（自带校验与相机自动调整），并在本机或渲染节点上渲染成视频。

验证环境：**Blender 5.2.2 LTS**（Python 3.13）、Windows。端到端实测：3 个场景 × 4 个模板 = 20 条序列，产出 20 套 MP4/JSON/TXT。代码同时兼容 Blender 4.x / 3.6 的相关 API 差异（见 [版本兼容](#版本兼容)）。

> 英文原文见 [README.md](README.md)。本文是压缩后的中文版：术语、面板名、字段名、命令行参数、文件名一律保留英文，方便与 Blender 界面逐字对照。

---

## 目录

1. [功能一览](#功能一览)
2. [安装](#安装)
3. [插件使用](#插件使用)
4. [项目文件夹（输出结构）](#项目文件夹输出结构)
5. [无头生成（CLI）](#无头生成cli)
6. [无头渲染](#无头渲染)
7. [配置参考](#配置参考)
8. [运动模板](#运动模板)
9. [复合运镜](#复合运镜compound-shots)
10. [相机校验与自动搜索](#相机校验与自动搜索)
10. [角色（Character）](#角色character)
11. [渲染农场注意事项](#渲染农场注意事项)
12. [测试](#测试)
13. [架构](#架构)
14. [版本兼容](#版本兼容)
15. [已知限制](#已知限制)

---

## 功能一览

| 阶段 | 入口 | 结果 |
|---|---|---|
| 批量加载场景 | 面板 **或** `motion_pipeline_cli.py` | 一次一个 `.blend`，原始文件永不被修改 |
| 解析运动模板 | 任意模板 JSON | 通用解析器，不硬编码任何运动类型 |
| 相机校验 | 默认开启 | 遮挡、穿模、取景、跳变、裁剪面 |
| 相机自动搜索 | 校验失败时 | 球面候选 + 加权评分 |
| 序列生成 | 面板 **或** CLI | 独立序列（只存相机动画，约 150 KB/条） |
| 视频渲染 | `render/render_sequences.py` | 每序列 MP4 + JSON + 相机轨迹 TXT |

生成矩阵为 **场景 × 运动模板 × 相机 × 角色 × 角色动画**；关闭角色时该维度折叠为单个"无角色"项。

---

## 安装

### 作为 Blender 插件

```powershell
# Windows
Copy-Item -Recurse blender_camera_motion_pipeline "$env:APPDATA\Blender Foundation\Blender\5.2\scripts\addons\"
```

```bash
# Linux
cp -r blender_camera_motion_pipeline ~/.config/blender/5.2/scripts/addons/
```

然后 **Edit — Preferences — Add-ons — Motion Pipeline — Enable**；3D 视图按 <kbd>N</kbd> 打开 **Motion Pipeline** 标签页。首次启用会自动发现模板文档并预填面板。

### 仅无头使用

无需安装，直接指向脚本即可：

```bash
blender -b -P /path/to/blender_camera_motion_pipeline/motion_pipeline_cli.py -- --help
blender -b -P /path/to/blender_camera_motion_pipeline/render/render_sequences.py -- --help
```

两个脚本都会自己定位插件包，放在包里、放在包旁边、或放进项目文件夹里都能跑。

### 插件文件夹可以改名

包内所有模块都用相对 import，所以文件夹叫 `blender_motion_pipeline`、`blender_camera_motion_pipeline` 或别的名字都不影响插件本体。两个独立脚本（CLI、无头渲染器）和测试套件需要按名字 import 插件包，它们按路径加载 `_bootstrap.py`，由它找到包根并把历史名字映射过去——改名后重新打包即可，两个入口照样工作。

---

## 插件使用

面板顺序：**Quick actions**、Scenes、Character、Motion templates、Camera validation、Sequence output、Local render、Actions、Status。

### Quick actions

日常只用两个按钮，按钮下方一行说明它将对什么生效：

| 按钮 | 作用 |
|---|---|
| **Generate sequences** | 等同下方的 `Start generation`；运行中变为 **Stop generating** |
| **Render video** | `Sequence root` 已设置且列表为空时自动加载序列列表，然后渲染所有 pending 序列；渲染中变为 **Stop render** |

场景列表和两个输出位置配置一次，之后这两个按钮就是全部流程。

面板运行由 `bpy.app.timers` 回调驱动，每一步都可能打开另一个 `.blend`；**打开文件会清空 Blender 的 Python timer 注册表**，所以每个回调结束都会重新注册自己，operator 与面板绘制路径也会顺手修复 timer。否则第一次打开场景就会杀掉自己的驱动，永远停在 `opening <scene>` 却仍显示 `running`。

### 设置会被记住

面板设置存在场景上（`scene.mpp`），是**按文件**的：一次运行会打开队列里每个 `.blend`，你配置的场景被替换后，下一个场景会从默认值开始——实测**14 个字段里有 11 个会丢失**。

现在配置同时保存在 `.blend` 之外：`<Blender config>/blender_motion_pipeline/panel_settings.json`，并在场景切换时自动恢复：

* **Remember these settings**（或直接按 *Generate sequences* / *Start generation*，它会在第一个场景打开前先快照）负责写入。
* 活动场景变化时（打开文件、批量运行、甚至 *File > New*）1 秒内自动恢复。
* **Use my settings** 强行套用到当前场景；垃圾桶图标清除。
* **自带配置的 `.blend` 保持自己的配置**：只有从未配置过的场景才会被填充，不会覆盖你精心调好的文件。
* 记住的内容：整个 `BatchConfig`（模板、输出、校验、搜索、渲染默认值……）、面板专属字段（相机选择、浏览目录、过滤器）、本地渲染选项、已排队场景列表。

用环境变量 `MPP_PANEL_SETTINGS` 可换成别的文件（便携配置或隔离测试——测试套件就指向临时文件，绝不碰你的真实配置）。`tests/probe_settings_persistence.py` 会走完整流程并打印哪些内容存活。

### 各面板要点

* **Scenes**：`Scene file`+`Add file` 添加单个文件、`Folder`+文件夹按钮扫描目录（可递归）、`Missing only` 只看有问题的行、`Remove selected`/`Clear list`/`Save list`/`Load list` 管理队列；重复项和不存在的文件会被拒绝并说明原因。
* **Character**：`Character mode`（无角色 / 只出角色序列 / 两者都要）、`Assets`/`Animations` 指向角色库（见 [角色](#角色character)）；`Import status` 如实报告 provider 能力，不可用时明确说明，无角色流程照常运行。
* **Motion templates**：模板 JSON 路径（**加载时预填**，永不为空：优先项目自己的模板文档，插件自带的副本只在没有模板集的机器上兜底），空时还有 **Use the default template set** 按钮；`Motion filter` 支持逗号分隔的 id 或 glob（如 `dolly_*, pan_left_*`）；`First frame`/`FPS`/`Interpolation` 控制时间轴。
* **Camera validation**：`Validate cameras`、`Sample step`、`Minimum clearance`、`Blocked-shot distance`、`Max move/turn per frame`、角色可见性/穿模检查，以及 `Auto-adjust camera`（球面搜索的全部参数）。
* **Sequence output**：**Project folder**、`Save validation report`、`Overwrite existing`、`Reuse existing sequences`、`Cameras`（`all`／名字／索引），以及记录给渲染器的渲染默认值（engine、samples、fps、视频格式、轨迹采样、**Sequence resolution**）。这些都会写进每条序列的 `sequence_config.json`，之后无头渲染可原样复现。
* **Sequence resolution** 是预设列表而非自由数字，标签直接写明像素：*720p (1280×720)*（默认）、*1080p (1920×1080)*、*1K square (1024×1024)*、*2K (2048×1080)*、*4K (3840×2160)*，另有 **Follow the source scene** 与 **Custom size**。渲染时的分辨率优先级（高到低）：**命令行/面板覆盖** → **序列自身的记录**（仅当序列指定了）→ **已加载场景**；渲染报告与每序列日志都会写明来源（`resolution_source: sequence | command line | scene`）。
* **Local render**：不离开 Blender 渲染序列，且不碰你当前打开的文件——每条序列都由一个后台 Blender 进程跑同一个独立渲染器，与渲染农场走完全相同的代码路径。可设 `Sequence root`/`Sequence folder`、`Save to`（`Sequence root` 指向项目的 `sequence/` 时默认写进项目的 `video/`）、`Flat output`、`Quality`（engine 下拉、分辨率/FPS/samples 各自带勾选覆盖、Cycles device、容器与编码、质量预设、可选 PNG 序列）、`Check only`、`Render`/`All`/`Stop render`；`panel_render.log` 记录每条命令与结果。
* **Actions**：`Check configuration`、`Validate scenes`、**`Start generation`**、`Stop task`、`Open project folder`、`View error report`、`Export/Import configuration`、`Reset to defaults`。
* **Status**：并排显示生成与渲染状态；状态存在插件 preferences 里，因此运行中替换了场景也照样能看到进度。

---

## 项目文件夹（输出结构）

一次运行写出**一个项目文件夹**：**只有数据，没有代码**——序列树 + 序列回放所需的场景副本。渲染器在渲染镜像里（`/opt/mpp/blender_camera_motion_pipeline`），`render-all.sh` 会优先用项目里的、找不到就用镜像自带的，所以项目里再放一份只会白白多出 ~2 MB 并产生两个版本。

```text
D:\projects\                         <- 你在面板里选的文件夹（Project folder）
└── blender_camera_20260213\         <- 运行时创建（同一天重跑复用）
    ├── project.json                 这个项目是什么 + 渲染命令 + 场景映射
    ├── RENDER_README.md             给渲染节点看的同样的命令
    ├── scene\                       每个源 .blend 的副本（原样复制，贴图不压缩）
    │   └── room001.blend
    ├── video\                       渲染输出
    └── sequence\                    序列树（渲染输入）
        ├── batch_config.json        本次运行的有效配置
        ├── batch_report.json        每场景/每序列结果
        ├── manifest.json            全部序列 + 全部失败的汇总
        └── room001\dolly_in_01_standard\sequence_000001\
            ├── sequence_config.json       生成器的决定
            ├── sequence_000001.json       相机轨迹 + 元数据 + 动画
            ├── sequence_000001_camera.txt 逐帧相机轨迹
            ├── validation_report.json     逐帧指标 + 搜索日志
            └── generation_log.txt         该序列的逐步日志
```

渲染这个文件夹（镜像内）：

```bash
render-all.sh <项目>/sequence            # 视频默认写到 <项目>/video
render-all.sh <项目>/sequence <输出目录>  # 也可以指定输出到别处（建议放挂载盘）
```

不用镜像、只用 Blender + 插件包时：

```bash
blender -b -noaudio --factory-startup \
  -P /opt/mpp/blender_camera_motion_pipeline/render/render_sequences.py -- \
  --input-root <项目>/sequence --output-root <项目>/video --recursive
```

CLI 的 `--sequence-root <dir>` 会把 `sequence/` 的内容直接写进 `<dir>`（不要项目外层），供需要精确路径的脚本使用。序列编号在每个 motion 文件夹内从 `sequence_000001` 重新开始，因此单个 motion 文件夹自洽，重跑某个 motion 不会影响另一个的编号。

**序列只存动画，不存场景副本。** 每条序列把生成出来的相机动画存下来（约 150 KB，而不是几百 MB），渲染器再把它回放到 `scene/` 里的场景副本上：

| | 在 265.9 MB 参考场景上实测 |
|---|---|
| 源场景 | 265.9 MB（本身已压缩） |
| 每条序列一份场景副本（已移除的模式） | 压缩 264.9 MB / 未压缩 610.6 MB |
| **每条序列的动画负载** | **约 150 KB** |
| 项目里那份场景副本携带的 packed 贴图 | 172.3 MB |
| 几何（2.29 M 顶点 / 3.3 M 面） | 其余部分 |

因此 17 条序列的项目只需一份场景副本 + 约 2.5 MB 序列，而不是 4.4 GB 的重复场景。

负载记录的是**实际打的关键帧值**——parent 空间 `location`、`rotation_quaternion`、`scale`、相机数据的 `lens`，逐帧——外加插值方式和烘焙时被 mute 的约束，所以回放不会相对生成器校验过的路径漂移。`tests/probe_animation_only_equivalence.py` 对比回放序列与生成器记录的轨迹：W2C 矩阵一致到 `1e-4`（距原点 431 m 处的单精度残差）。

代价是场景必须跟序列一起走——这正是项目文件夹做的事：放在 `scene/`，并记录 `source_scene_rel`，搬走也照样能找到。场景内部的贴图仍指向生成它们的机器：要么用 `--path-map` 映射，要么跑一次 `pack_textures.py` 打包进副本。

### 渲染输出

```text
<project>\video\
├── render_report.json
└── room001\dolly_in_01_standard\sequence_000001\
    ├── sequence_000001.mp4
    ├── sequence_000001.json          JSON 详情
    ├── sequence_000001_camera.txt    相机轨迹
    ├── sequence_config.json          从源序列复制
    └── sequence_000001_render_log.txt
```

JSON 详情的首段与 Unreal 参考脚本（`movie_render.py`）的键保持一致，便于既有消费方复用（`level_name`、`sequence_name`、`video_id`、`video_path`、`frame_count`、`camera_trajectory`、`text_prompt`），第二段是渲染细节（`status`、`render.engine`/`resolution`、`trajectory_export` 等）。

相机轨迹 TXT 表头固定 19 列：`frame focal_length d1 d2 d3 d4 d5 r00 r01 r02 tx r10 r11 r12 ty r20 r21 r22 tz`，`d1..d5` 为保留畸变位、恒为 0。

矩阵是 **Blender 自身的 world-to-camera 矩阵**，所以可视化工具可以**直接求逆**把相机画成场景里的真实姿态（`tools/visualize_trajectory.py` 就是这么做的：`inv(w2c)` → 视锥沿 local `-Z`、up 为 local `+Y`、世界 `Z` 向上，**不需要任何额外参数**）：

* 三行就是相机自身三个轴在世界中的方向：`row0 = +X`（右）、`row1 = +Y`（上）、`row2 = +Z`（后）——因此**视线方向是 `-row2`**；
* `det(R) = +1`（真正的旋转，`inv` 得到的是相机而不是镜像），且 `inv([R|t])` 等于 Blender 的 `matrix_world`（两条都有测试）；
* 相机**前方**的点在相机坐标系里 `z` 为**负**（Blender 沿 local `-Z` 看）；
* 若要 OpenCV 的 `+Y` 向下 / `+Z` 向前约定，把旋转与平移的第 1、2 行同时取反（`diag(1,-1,-1)`），行列式仍为 `+1`。只翻第 1 行（本导出以前为了模仿参考实现的做法）会得到行列式 `-1` 的镜像矩阵，任何可视化工具都无法正确求逆。

---

## 无头生成（CLI）

```bash
# 从配置文件出发，扫描一个场景目录
blender -b -P motion_pipeline_cli.py -- \
    --config batch.json --scene-dir "D:\scenes" --recursive \
    --output-root "D:\projects"

# Blender 已经打开的那个场景
blender -b "D:\scenes\room001.blend" -P motion_pipeline_cli.py -- \
    --include-current --output-root "D:\projects"

# 选模板、限定帧范围
blender -b -P motion_pipeline_cli.py -- \
    --scenes "D:\scenes\room001.blend" --output-root "D:\projects" \
    --templates "E:\UE\...\camera_motion_templates.json" \
    --motion-filter "dolly_*" --frames 1:120 --fps 24

# 只要裸序列树（不要项目外层）
blender -b -P motion_pipeline_cli.py -- \
    --scenes "D:\scenes\room001.blend" --sequence-root "D:\generated"

# 预演，不写任何东西
blender -b -P motion_pipeline_cli.py -- --config batch.json --scene-dir "D:\scenes" --dry-run

# 打印有效配置
blender -b -P motion_pipeline_cli.py -- --print-config
```

`--output-root` 是**项目文件夹**（与面板一致，里面创建带日期的项目目录）；`--sequence-root` 保持"直接写序列树"的历史行为。退出码：`0` 成功、`1` 生成问题、`2` 配置/输入错误。

`--no-sequence-blend` 仍然接受但**不做任何事**：序列已经不再写场景副本，没有东西可关。

---

## 无头渲染

项目文件夹只有数据，渲染器来自渲染镜像（`docker_blender/`）。镜像里一条命令就够：

```bash
render-all.sh <项目>/sequence             # 视频写到 <项目>/video
render-all.sh <项目>/sequence <输出目录>   # 输出到别处（建议挂载盘）
```

它读 `<项目>/sequence/**/sequence_config.json`，默认用每条序列记录的设置（引擎/分辨率/fps/帧范围），并打印预检、清单、日志和汇总。退出码：`0` 全部成功、`1` 有失败、`2` 预检/用法问题、`3` 没有可渲染的序列。

没有镜像时，用本包的渲染器（同一个文件）：

```bash
blender -b -noaudio --factory-startup -P render/render_sequences.py -- \
    --input-root "<project>/sequence" \
    --output-root "<project>/video" \
    --recursive
```

```bash
# 渲染 Blender 已打开的文件
blender -b sequence_000001.blend -P render/render_sequences.py -- --output "D:\render_output"

# 只渲染一部分
blender -b -P render/render_sequences.py -- \
    --input-root "D:\generated" --output-root "D:\render_output" \
    --scene-filter "room*" --motion-filter "dolly_*"

# 质量与引擎覆盖；不指定时用序列自己的记录（engine/samples 总是，分辨率仅在序列指定时）
blender -b -P render/render_sequences.py -- \
    --input-root "D:\generated" --output-root "D:\render_output" \
    --engine CYCLES --device GPU --samples 128 --resolution-x 1920 --resolution-y 1080

# 检查输入/资产/输出但不渲染；列出将要渲染的内容
blender -b -P render/render_sequences.py -- --input-root "D:\generated" --dry-run
blender -b -P render/render_sequences.py -- --input-root "D:\generated" --list

# 把生成机器上的资产路径映射到渲染节点
blender -b -P render/render_sequences.py -- \
    --input-root /mnt/gen --output-root /mnt/out \
    --path-map "E:\scenes=/mnt/e/scenes" --path-map "E:\textures=/mnt/e/textures"
```

退出码：`0` 成功（含"全部都渲染过了"）、`1` 至少一条序列失败、`2` 没找到序列、`3` 未预期错误。

`--workers N` 会拉起 N 个 Blender 进程（各一批）。它只在瓶颈是"每进程"而非"每 GPU"时才有收益：EEVEE 在 GPU 上逐帧渲染，同一张卡上多个 worker 会抢 VRAM——在重场景上（3.3 M 面、34 盏投影灯、4.2 GB 常驻）3 个 worker 让每帧慢约 10 倍而不是吞吐涨 3 倍。单 GPU 机器上 `--workers 1` 最快。

### 在另一台机器（Linux 渲染节点）上渲染

项目文件夹是可搬移的，所以渲染节点只需要一个 Blender——**不用装插件、也不用写路径映射**：

```bash
# 传过去
scp -r blender_camera_20260213 user@node:/data/proj/
# 在那边渲染（镜像内；输出目录要落在挂载盘上）
ssh node
render-all.sh /data/proj/blender_camera_20260213/sequence /data/out/video
```

为什么不需要任何参数：每条序列通过 `source_scene_rel`（`scene/<名字>.blend`）找到自己的场景，而 `project.json` 标出了项目根在哪。记录下来的 `source_blend` 是生成机器的绝对路径（`E:/…`），在节点上不可能存在——真正把镜头带过去的是那个相对路径。如果文件夹被重新组织过，用 `--project-root <dir>` 指明位置；此时 `--path-map` 只需要用于这些 `.blend` **内部**的资产。

`--list` 会逐条打印它将打开哪个场景文件，找不到时标记为 `scene missing`——在新节点上这是最合适的第一条命令：

```bash
blender -b -P render/render_sequences.py -- --input-root sequence --output-root video --list
```

### 让项目文件夹不依赖生成机器

序列本身没问题（它存的是数据不是路径），项目中 `scene/` 里的 `.blend` 副本是唯一还指向生成机器的东西（贴图、链接库、缓存）：

```bash
# A. 在渲染节点上映射路径（可重复；对场景和资产都生效）
render-all.sh "<project>/sequence" --path-map "E:/UE/DataGenScenes=/mnt/data/DataGenScenes"

# B. 一次性把外部文件打包进副本，之后整个文件夹自带全部依赖
blender -b -P /opt/mpp/blender_camera_motion_pipeline/render/pack_textures.py -- \
    --scene-root "<project>/scene"
```

`pack_textures.py` 会打开每份场景副本、打包其外部文件、压缩存回原处，并在 `scene/` 旁写 `pack_report.json`，列出打包了什么、哪些找不到（找不到只报告，不会中断）。常用参数：`--scene <file>`、`--dry-run`、`--list`、`--no-compress`、`--report <path>`。

---

## 配置参考

一份 JSON 同时驱动面板与 CLI，见 `config/example_config.json` 与 `config/schema.json`。

```json
{
  "schema_version": 1,
  "batch": {
    "output_root": "D:/projects",
    "mode": "none",
    "overwrite": false, "resume": true,
    "save_validation_report": true,
    "character_asset_root": "", "animation_asset_root": "",
    "character_provider": "auto",
    "path_mappings": [{"from": "E:/UE", "to": "/mnt/e/UE"}]
  },
  "motion": {
    "template_path": "E:/UE/.../camera_motion_templates.json",
    "template_names": [], "template_overrides": {},
    "frame_start": 0, "frame_scale": 1.0, "interpolation": "BEZIER",
    "unit_scale": { "fps": 24.0, "rotation_order": "XYZ" }
  },
  "validation": {"enabled": true, "sample_step": 10, "clearance": 0.25},
  "search": {"enabled": true, "min_radius": 0.2, "max_radius": 3.0,
             "candidate_count": 64, "max_output_candidates": 1},
  "render": {"engine": "BLENDER_EEVEE", "samples": 32,
             "resolution_x": 1280, "resolution_y": 720, "fps": 24.0,
             "video_format": "mp4", "codec": "H264"},
  "scenes": [{"path": "E:/scenes/room001.blend", "enabled": true}]
}
```

键名接受 `snake_case`、`camelCase`、`kebab-case`、`UPPER_CASE`；未知键只给警告不报错，因此新版本写的配置在旧版本上仍能跑——包括已移除的 `batch.save_sequence_blend`（不再有序列存场景副本）和 Unreal 时代的 `motion.unit_scale` 映射键，现在都被忽略。

---

## 运动模板

生成器读任何与参考文档同形状的文档，运动中没有任何类型被硬编码，加一个运动就是加一条 JSON。

**发现顺序**：`motion.template_path`（面板字段 / `--templates`，不可读则报错）→ `motion.template_data`（配置内联数组）→ 环境变量 `$MOTION_PIPELINE_TEMPLATES` → 自带 `templates/camera_motion_templates.json` → 已知参考位置（`E:\UE\DataGenScenes\Plugins\MetaHumanScenePipeline\Templates` 等，仅警告）→ 内置极简集合兜底。

**接受的形状**：`{"templates": [...]}`、`{"motion_templates": [...]}`、`{name: {...}}`、单个模板对象都可以；别名同样接受（`name`/`template` → `id`，`keyframes`/`samples`/`frames` → `keys`，`position`/`pos` → `location`，`angles`/`rot` → `rotation`，`lens`/`focal_length` → `focal`）。逐帧或逐模板的额外字段会保留在 `parameters` 里。

**坐标约定：Blender 坐标系，不做任何转换。** 模板用 **Blender 坐标**书写，数值**原样使用**：不换轴、不翻符号、不缩放单位。写的就是相机自身的局部变换，和你在 Blender 里直接给相机摆位姿一模一样。

| 模板量 | 含义 |
|---|---|
| `location` | 相机**自身坐标系**下的偏移，单位**米**：`+X` 右、`+Y` 上、`+Z` 后——即 **`-Z` 是前方**（前推 3 m 写 `[0, 0, -3]`） |
| `rotation` | `[rx, ry, rz]` 度，绕同样的局部轴：`rx` 俯仰、`ry` 左右转、`rz` 滚转画面；按 `motion.unit_scale.rotation_order` 组合（默认 `XYZ`，即 Blender 自己的顺序） |
| `focal` | 毫米 |

偏移是在**相机起始朝向**下施加的，所以"向右 0.5 m、向上 1.2 m、向前 3 m"就是 `[0.5, 1.2, -3]`，与相机在世界里朝哪无关。

以上每一条都被 `tests/probe_axes.py` 断言、并被 `tests/test_motion_templates.py` 锁定：`dolly_in` 确实把相机向前推、`pan_right` 确实向右转、`pedestal_up` 确实抬高、`truck_right` 确实向右平移、`roll` 确实只旋转画面不改变朝向。以 `hitchcock` 为例，模板是 `[0,0,-3.4]` 的纯前推，实测位移与相机 forward 轴夹角 **0.0°**、前向分量 **+3.400 m**、横向/纵向分量 0。

所以 `motion.unit_scale` 只剩时间轴设置（`fps`、`rotation_order`）。Unreal 时代的键（`location_scale`、`location_forward/right/up`、`yaw_axis`/`pitch_axis`/`roll_axis` 及各自符号）已删除；配置里若仍有这些键，会收到一条指明迁移脚本的警告并被忽略。

**把 Unreal 坐标的模板集迁移过来**（`location` 用厘米、`X` 前 `Y` 右 `Z` 上，`rotation` 是 `[roll, pitch, yaw]`、yaw 绕**世界**上轴）：离线转换一次即可：

```bash
python tests/migrate_unreal_templates.py --input templates.json --check     # 只报告
python tests/migrate_unreal_templates.py --input templates.json --in-place  # 原地转换并留备份
python tests/migrate_unreal_templates.py --input templates.json --output blender.json
```

规则：`location [forward, right, up]` 厘米 → `[right, up, -forward]` 米；`rotation [roll, pitch, yaw]` → 局部 `[pitch, -yaw, -roll]`；id、帧号、焦距以及所有额外字段原样保留。脚本会拒绝转换"看起来已经是 Blender 坐标"的文档（米制偏移数值很小），除非显式 `--force`，所以误跑两次是安全的。

自带的 `templates/camera_motion_templates.json`、旁边的测试集、以及项目自己的参考文档都已用该脚本迁移；它们旁边的 `*.unreal_backup.json` 是迁移前的原件（永远不会被发现或加载——发现逻辑只认精确文件名）。

**烘焙契约**：生成器构造的是**世界空间**位姿（校验器和轨迹 JSON/TXT 记录的就是它），烘焙必须在 Blender 实际求值相机的空间里精确复现。`obj.location` 位于对象的 **parent** 空间，所以带 parent 的相机逐帧换算：

```
local_basis = inverse(matrix_parent_inverse) @ inverse(parent_world) @ world_pose
```

`parent_world` 在每个关键帧都从求值后的 depsgraph 重读，因为正是 parent 自身的动画带着挂在 rig 上的相机穿过场景。影响非零的约束会在烘焙时被 mute（并保持 mute）：打了关键帧的旋转无法在 `TRACK_TO`/`COPY_ROTATION` 生效时存活，留着会让渲染结果与校验过的路径不一致。

需要知道的后果：生成的运动**替换**相机自己的动画，锚定在相机起始位姿上。挂在运动 rig 上的相机因此保持这条世界路径，而不是继承 rig 的位移——主体穿过画面，而不是相机跟着走。轨迹文件、校验报告和视频描述的是同一条路径，这正是渲染器依赖的性质。`tests/probe_all_sequences.py` 扫整棵序列树，任何序列的求值路径与其自身轨迹偏差超过 1 cm 就失败；`tests/probe_bake_math.py` 用随机 rig、parent 偏移和位姿对照 `mathutils` 验证矩阵代数。

**序列锚点**：每条序列锚定在**它自己第一帧**（`motion.frame_start`，未设则用场景起始帧）的相机位姿，之后把场景恢复成作者状态——变换、镜头**以及** action。两半都重要：锚点帧每条序列前显式设置（否则批量里第一条锚在作者保存文件时的帧、其余锚在上一条结束处，同一模板会因批量位置不同而拍出不同镜头）；恢复时写入快照的**世界**矩阵、让 Blender 自行推导局部矩阵，再挂回作者的 action（把世界位移直接塞进 parent 空间的 `obj.location` 看似无害，实际会让相机沿动画列车多走 25.96 m）。

**覆盖模板**：

```json
"template_overrides": {
  "dolly_in_03_strong": {"location_scale": 0.5, "frame_scale": 2.0},
  "*": {"frame_offset": 10}
}
```

支持的补丁键：`keys`（整体替换）、`frame_scale`、`frame_offset`、`location_scale`、`focal_scale`、`focal`；其他键存为模板参数。

**支持的模板**：文档里有多少就支持多少。参考文档有 **80** 个模板、16 个家族：`dolly_in`、`dolly_out`、`fixed`、`hitchcock`、`pan_left`、`pan_right`、`pedestal_down`、`pedestal_up`、`roll`、`tilt_down`、`tilt_up`、`truck_left`、`truck_right`、`zoom_in`、`zoom_out`（各 5 个变体，`hitchcock` 10 个）。`--motion-filter` / 面板的 **Motion filter** 可按 id 或 glob 选子集。

---

## 复合运镜（时空复合）

一条复合镜头是在**空间与时间**上同时编排的：视频被切成若干 **分段**，每一段内可以**同时**执行多个**原子运镜**。两个原子运镜能否同时出现，取决于它们是否驱动同一个**轴（channel）**：

| channel | 驱动什么 |
|---|---|
| `yaw` | 绕相机自身上轴的旋转（`ry`）—— Pan，以及 Arc 的旋转部分 |
| `pitch` | 绕相机自身右轴的旋转（`rx`）—— Tilt |
| `roll` | 绕相机视线轴的旋转（`rz`）—— Roll |
| `lateral` | 沿相机自身右轴平移（`x`）—— Truck，以及 Arc 的横移部分 |
| `vertical` | 沿相机自身上轴平移（`y`）—— Pedestal |
| `depth` | 沿相机视线平移（`z`）—— Dolly In/Out |
| `focal` | 焦距（mm）—— Zoom In/Out |

所以 `Pan right + Tilt down + Truck left` 是合法的三运镜分段，而 `Zoom In + Zoom Out`、`Pedestal up + Pedestal down` 会被拒绝（同一轴）；`Arc` 自己就驱动 lateral+yaw 两个轴，因此不能与 Pan / Truck 同时出现。

### 原子运镜模板

`templates/atomic_motion_templates.json` 共 49 条：Pan（左/右）、Tilt（上/下）、Roll（顺/逆时针）、Truck（左/右）、Dolly In/Out、Pedestal（上/下）、Arc（顺/逆时针）、Zoom In/Out——**每种都有 slow / medium / fast 三档**——加一条 `static`。可用 `python tests/make_atomic_templates.py` 重新生成；每条都是普通模板，关键帧是**1 秒的斜坡**，因此它的增量就是“每秒速率”：

| 原子 | slow | medium | fast |
|---|---|---|---|
| Pan | 8 °/s | 18 °/s | 40 °/s |
| Tilt | 5 | 12 | 26 |
| Roll | 4 | 10 | 22 |
| Truck | 0.25 m/s | 0.6 m/s | 1.3 m/s |
| Dolly | 0.3 | 0.7 | 1.5 |
| Pedestal | 0.15 | 0.35 | 0.75 |
| Arc | 0.25 m/s 横移 + `v/4 m` rad/s 偏航 | 0.6 | 1.3 |
| Zoom | 4 mm/s | 10 mm/s | 22 mm/s |

因为是速率，分段**时长**只影响运动走多远，不影响看起来的快慢：“Pan left, medium”在 0.5 s 和 6 s 的分段里都是 18 °/s。像 `hitchcock`、`fixed` 这类“整个镜头”故意不在词汇表里——复合镜头是用原子搭出来的。

### 配置（面板：*Sequence output* → *Compound shots*）

| 设置 | 含义 |
|---|---|
| **Max moves at once** | 同一时刻最多同时出现几种运镜（1-5） |
| **Max segments** | 一条视频最多分几段；每段至少 **0.5 s**，因此短视频会自动压低上限 |
| **Sequences per camera** | **单台相机**输出多少条复合序列（`sequences_per_camera`）。人物/动画变体**不会**乘上去，而是分摊到这些序列上：4 个变体、总量设 2，仍然只产出 2 条 |
| **Random counts** | 开：上面两项是每条序列随机取值的**上限**；关：每段固定那么多种、整条固定那么多段。选哪些运镜与速度始终随机（由 **Random seed** 可复现） |
| **Video length** | `Fixed`（每条都是这个时长）或 `Random range`（每条在 Min/Max 之间自己抽）；帧范围 = 时长 × fps |
| **Compound output** | 与单运镜镜头一起生成 / 只生成复合 / 只生成单运镜 |
| **Atomic templates** | 词汇表文档；留空则用自带的 |

CLI 同样对应：`--compound-simultaneous`、`--compound-segments`、`--compound-random/--no-compound-random`、`--compound-templates`、`--duration`、`--duration-mode`、`--duration-min/--duration-max`、`--compound-output`、`--compound-seed`。`--dry-run` 会在生成前打印分段上限、时长范围与一个示例计划。

### 产出什么

每台相机一条序列，放在很短的 `combo/` 目录下。除通常的 sidecar 与轨迹文件外，还会写出**镜头报告**（`<sequence>_motion_plan.json`），渲染器会把同一份文件复制到视频旁边：

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

时间是 `1/fps` 的整数倍，因此报告与视频完全对齐；各段首尾相接且覆盖整条视频。完整计划（分段、帧范围、原子速率、种子）同时记在序列 JSON 的 `extra.motion_plan` 里；`tests/probe_template_contract.py` 会把每份计划重新展平并逐帧对比实际轨迹——纯计划树不需要任何模板文档。


---

## 相机校验与自动搜索

校验采样**首帧、末帧、每 `sample_step` 帧、以及 `validation.extra_sample_frames`**——绝不只是起始帧。

| 检查 | 字段 / reason |
|---|---|
| 相机本体不碰几何 | `clearance`，`camera_clipping` |
| 相机不在网格内部 | `inside_epsilon`，`camera_inside_geometry` |
| 相机被围住/镜头被挡 | `obstruction_distance`，`camera_obstructed` |
| 角色在画面内且可见 | `check_character_visibility`、`min_character_visible_ratio`，`character_invisible` / `character_unframed` |
| 角色穿模 | `check_character_overlap`，`character_overlap` |
| 位置/旋转异常跳变 | `max_position_jump`、`max_rotation_jump_deg`，`position_jump` / `rotation_jump` |
| 非有限/退化值 | `illegal_value` |
| 裁剪面与场景范围 | `min_clip_start`、`max_clip_end`，`clip_range_invalid` |

射线来自求值 depsgraph 上的 `scene.ray_cast`，因此骨骼形变与修改器都算数。角色自身的网格从相机避让与角色可见性两项检查中**都被排除**：相机不该被它正在拍的人挡住，角色也不该遮住身后的墙。`jump_gap_scale`（默认 `sqrt`）在采样跨多帧时按 `sqrt(frame_gap)` 放宽跳变阈值，避免粗粒度 `sample_step` 把一次合法的多帧移动报成逐帧跳变。

校验失败且 `search.enabled` 为真时，会在原相机周围的球面上取候选位置：规则方位/俯仰网格、Fibonacci 球、同心径向壳层、带种子的随机填充，以及可选的小角度朝向微调（对准角色）与焦距步进。候选按此排序：

```text
penalty = w_distance   · offset / 10 m
        + w_clipping   · (clipping + inside + overlap + jump + illegal + clip_range 比例)
        + w_invisible  · 不可见帧比例
        + w_occlusion  · 被遮挡帧比例
        + w_rotation   · 旋转变化 / 90°
        + w_focal      · 焦距变化 / 100 %
```

通过全部硬性检查且最保守（漂移最小）的候选胜出，**然后**才烘焙关键帧。若没有候选通过，该组合记录为**失败**并写 `failure_report.json`——绝不输出明知不好的序列。`search.enabled = false` 时，校验失败同样记为失败。

---

## 角色（Character）

`CharacterProvider` 是需求里要求的可插拔适配器，接口包含 `status`、`list_characters`、`list_animations`、`import_character`、`place_character`、`apply_animation`、`validate_character_placement`。

| Provider | 状态 | 说明 |
|---|---|---|
| `blender` | **可用** | 追加 Blender 原生角色 `.blend` 库、绑定 armature action（正确处理 Blender 4.4+ action slots）、把角色落到场景地面、校验摆放 |
| `null` | 设计上不可用 | 接口完整、没有资产；说明原因并让无角色流程继续，绝不假装成功 |
| `unreal_metahuman` | 未实现 | 说明平台边界（MetaHuman blueprint 无法被 Blender 直接加载，需要额外的 retarget/export 步骤） |
| `auto`（默认） | 有库则选 `blender`，否则 `null` | |

角色库清单（`manifest.json`）包含 `characters[]`（`id`、`blend_path`、`object_name`、`collection`、`animations`）与 `animations[]`（`id`、`blend_path`、`action_name`、`frame_start`、`frame_end`、`applies_to`）。把 **Character — Assets** 指向含 `manifest.json` 的目录（或该文件本身）；清单内路径相对清单解析，库因此可搬移。只有 `.blend` 没有清单的目录会被扫描并报告为降级库（有角色、无动画）。

**当前状态：角色模块已完整接线并针对 fake 测试，但本工作区没有真实角色资产，因此没有用真实 MetaHuman 或 Blender rig 产出过角色序列。** 见 [已知限制](#已知限制)。

---

## 渲染农场注意事项

* 无 GUI、无点击：一切来自参数或配置文件。
* `--dry-run` 解析输入、输出与资产而不渲染；`--asset-report` 写出缺失资产清单；`--path-map FROM=TO` 重写存储路径并逐项记录重映射。
* 渲染可续跑：已完成的视频会被识别（包括 Blender 的 `<name>_<start>-<end>.mp4` 命名，会被规范成 `<name>.mp4`）并在无 `--overwrite` 时跳过；全部渲染完的树以 `0` 退出并明确说"nothing to do"。
* `render_report.json` 汇总每条序列的 rendered/failed/skipped；非零退出码告诉调度器有失败。
* `--workers N`（N 个 Blender 进程，各一批）、`--device CPU|GPU`、`--samples`、`--engine` 覆盖 GPU/CPU 选择。
* Windows 与 Linux 路径都可用：产物一律写正斜杠，`--path-map` 不区分分隔符，存储的绝对路径原样保留以便追溯。

---

## 测试

```bash
# 全量（纯 Python 套件 + Blender 套件）—— 241 个用例
blender -b -P blender_camera_motion_pipeline/tests/run_blender_tests.py

# 只跑纯套件，不需要 Blender —— 144 个用例
python blender_camera_motion_pipeline/tests/run_blender_tests.py

# 单个套件也能独立运行
blender -b -P blender_camera_motion_pipeline/tests/test_blender_integration.py
blender -b -P blender_camera_motion_pipeline/tests/test_animation_api.py
python blender_camera_motion_pipeline/tests/test_project_layout.py

# 端到端验收（真实 80 模板文档，走 CLI + 渲染器）
blender -b -P blender_camera_motion_pipeline/tests/verify_end_to_end.py

# 关键探针
python blender_camera_motion_pipeline/tests/probe_axes.py
blender -b -P blender_camera_motion_pipeline/tests/probe_bake_math.py
blender -b -P blender_camera_motion_pipeline/tests/probe_settings_persistence.py
blender -b -P blender_camera_motion_pipeline/tests/probe_icons.py
blender -b -P blender_camera_motion_pipeline/tests/probe_all_sequences.py -- "<sequence root>"
blender -b -P blender_camera_motion_pipeline/tests/probe_blend_size.py -- "<scene.blend>"
blender -b -P blender_camera_motion_pipeline/tests/probe_render_cost.py -- "<sequence.blend>"

# 每条序列是否与生成它的模板数值一致（纯 Python）
python blender_camera_motion_pipeline/tests/probe_template_contract.py -- \
    --sequence-root "<生成的树>" --templates "<templates.json>"
```

环境变量：`MP_KEEP_TEST_OUTPUT=1` 保留集成测试产物、`MP_KEEP_E2E=1` 保留端到端产物、`MP_TEST_TRACEBACK=1` 打印完整 traceback。`tests/_boot.py` 让套件与探针在插件文件夹被改名后仍能独立 import 插件包。

### 本机结果（Blender 5.2.2 LTS / Windows）

| 套件 | 用例 | 结果 |
|---|---|---|
| `test_path_utils` | 16 | 通过 |
| `test_config` | 18 | 通过 |
| `test_project_layout` | 15 | 通过 |
| `test_motion_templates` | 33 | 通过 |
| `test_motion_composite` | 23 | 通过 |
| `test_camera_validation` | 39 | 通过 |
| `test_animation_api` | 10 | 通过 |
| `test_addon_lifecycle` | 10 | 通过 |
| `test_render_workflow` | 18 | 通过 |
| `test_blender_integration` | 59 | 通过 |
| **合计** | **241** | **通过** |

`tests/static_check.py` 另外检查全包无未使用 import、无遗留调试标记；其中一个集成用例用桩 layout 驱动**每个面板的 `draw()`**，避免"面板读了已不存在的属性、直到用户打开侧栏才崩"。

端到端验收：**10/10 阶段**通过——构建 3 个夹具场景 → CLI 预演 → 用真实 80 模板文档生成 20 条序列 → 项目文件夹检查（只有 `sequence/` + `scene/` + `video/`，数据齐全且自描述）→ 产物布局检查 → **用插件包（生产环境是镜像内）的渲染器渲染出 20 个视频到 `<project>/video`** → 每条视频都有配套 JSON + 轨迹 TXT → `ffprobe` 确认 H.264 与 81 帧 → 重跑渲染器跳过已完成序列并退出 0 → `--list` 枚举状态。日志见 `E2E_ACCEPTANCE_20260919.txt`。

---

## 架构

```text
blender_camera_motion_pipeline/
├── __init__.py / registration.py / preferences.py   插件入口、注册顺序、偏好与状态
├── _bootstrap.py      让插件包无论文件夹叫什么都能被 import
├── properties.py      场景 PropertyGroup ↔ BatchConfig
├── operators.py       面板 operator（含生成/渲染 timer）
├── panels.py          侧栏面板与两个 UIList
├── motion_pipeline_cli.py   无头生成 CLI
├── config/            models / defaults / panel_state / schema.json / example_config.json / 内置模板
├── core/              编排（需要 bpy）
│   ├── scene_loader.py / blender_context.py
│   ├── sequence_generator.py   单条序列：校验 → 搜索 → 烘焙 → 写盘
│   ├── batch_runner.py         场景 × 运动 × 相机 × 角色
│   ├── project.py              一次运行写出的精简（纯数据）项目文件夹
│   ├── camera_animation.py     动画负载与回放
│   ├── sequence_manager.py     输出树的只读视图
│   └── ui_task.py              可取消的增量 timer 状态机
├── camera/            motion_templates / scene_context / camera_validator / camera_search / camera_export
├── character/         base_provider / null_provider / blender_provider / library
├── io/                path_utils / json_io / manifest / resource_check
├── render/            render_sequences.py / pack_textures.py / render_runner.py / metadata_exporter.py
├── utils/             logging_utils / task_control / animation / version
└── tests/             harness + 套件 + 探针（含 _boot.py）
```

代码遵循的设计规则：

* **核心与 UI 解耦**：`camera/`、`io/`、`config/`、`utils/` 以及 `core/` 的大部分不 import `bpy`，可单测、可被 CLI 复用；需要时才在函数内延迟 import。
* **先校验后改动**：候选只在内存里评分，只为胜者烘焙关键帧。
* **永不修改原始文件**：相机动画作用在相机数据块的**副本**上，作者的 action 留在原文件里，保存永远是 *Save As* 到项目里。
* **失败也是数据**：每次失败都变成 `failure_report.json`、清单条目和批次报告行，运行继续。
* **诚实状态**：无法完成工作时（`null` 角色 provider、缺失 ray caster、模板不可读）如实说明，其余流程照常。

---

## 版本兼容

在 Blender 5.2.2 上构建并验证；要求 Blender **3.6+**，无第三方 Python 依赖。以下 API 变化都被显式处理，每项都有测试：`image_settings.file_format` 受 `media_type` 门控（5.2+）、`Action.fcurves` 改为分层 action（5.0+）、action 需要 action slot 才生效（4.4+）、`scene.ray_cast` 需经 depsgraph 访问、模块 operator 不再注入 `bpy.ops`（5.2）、`PropertyGroup` 类不再作为 `bpy.types` 属性暴露（5.2）、`blender -b` 下没有 `Render Result` 缓冲、Blender 会在视频文件名后追加帧范围（渲染器会重命名回 `<id>.mp4`）。

---

## 已知限制

1. **本工作区没有真实角色资产。** 适配器完整并对 fake 测试过，`null`/`unreal_metahuman` 路径也被真实走过，但没有 MetaHuman 或 Blender rig 可用，因此角色序列未从真实资产产出过。
2. **几何测试基于射线与 AABB。** 单面薄片正面命中可靠、擦边可能漏；极密网格会让搜索变慢。避让沿 26 个方向采样，不做解析求值。
3. **场景是复制的，不是引用的。** 运行会把每个排队 `.blend` 复制进项目的 `scene/` 并从副本生成，代价是每个项目一份场景大小的磁盘（同一天重跑复用副本）。副本是**逐字节复制**：贴图不会被降采样或重新打包。副本内部的贴图在跑 `pack_textures.py` 或用 `--path-map` 之前仍指向生成机器。
4. **分辨率是渲染期决定。** 生成不改变场景尺寸，只把分辨率/fps/engine 记进 `sequence_config.json`，由渲染器应用；帧率写进序列文件（`scene.render.fps`），因为模板帧号是绝对的。
5. **焦距是唯一的镜头控制。** Blender 无法表达"以单位表示焦距"，所有焦距处理都是毫米；模板焦距低于 1 mm 会被截断，请求值仍保留在元数据里。
6. **轨迹符号约定。** TXT 与参考实现的 `R^T` 约定一致，因此相机**前方**的点 `z` 为正；每个文件头都写明，并有测试锁定。
7. **`--workers` 是顺序的多 Blender 进程**，不是分布式调度；多机分发不在范围内。
8. **多场景 `.blend` 只用活动场景**；同一文件里的其他场景会被 `Validate scenes` 列出但不参与生成。
9. **进度按序列而非按帧**：一条很长的序列不报告中间进度，`Stop task` 会等它结束。
10. **生成的运动替换相机自身动画**，锚定在相机起始位姿（见烘焙契约）。挂在运动 rig 上的相机保持模板的世界路径而非跟随 rig，主体会穿过画面——这是自洽的（轨迹、校验、渲染一致），但不等同于"在 rig 原有运动上叠加一个 dolly"。
11. **EEVEE 的开销来自场景本身**：34 盏投影灯 + 3.3 M 面的场景在 1280×720/32 samples 下约 23 秒/帧（RTX 4060 Laptop，GPU 利用率约 99%、常驻 4.2 GB，属 GPU 受限而非配置错误）。批量前用 `tests/probe_render_cost.py` 估算时间。

---

## 许可 / 出处

运动模板语义来自 Unreal 参考工程（`E:\UE\MetaHumanScenePipeline`、`E:\VSCode\CameraCtrl\movie_render.py`），仅作分析阅读，从未修改。
