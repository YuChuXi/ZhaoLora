# 就绪

根据当前任务工作目录生成训练用配置文本，供复制到训练配置或一键导出。

## 配置格式

顶栏格式切换段决定导出的配置文件集合与导出根目录：

| 格式 | 文件集合（由 `assets/configs/{格式}/` 下模板扫描得到） | 导出根目录（`.env`） |
|------|----------|----------------------|
| sd-scripts | `lora.toml` / `dataset.toml` / `prompt.txt` | `SD_SCRIPTS_CONFIG_ROOT` |
| diffusion-pipe | `train.toml` / `dataset.toml` | `DIFFUSION_PIPE_CONFIG_ROOT` |

导出写入 `{格式对应配置根}/{数据集名}/`；相对路径按 `web_annotator/` 目录解析。目标目录已存在配置时需确认覆盖。切换格式后自动重新生成，各文本区编辑内容随重建丢弃。

配置文本在渲染时即按 `services/ready.py:export_dir` 写出绝对路径，因此**预览文本与导出写入的文本完全一致**：导出逐字段以编辑后的文本为准，不做任何改写，仅在写入前校验 `.toml` 语法。缺少对应配置根环境变量时生成与导出均报 400。

配置文本由 `web_annotator/assets/configs/<格式名>/` 下的 Jinja2 模板渲染（`services/ready.py:_render_files`）：模板目录名即格式名，目录内 `*.j2` 去掉后缀即该格式的配置文件名；每个模板需要哪些变量由其自身声明决定（`jinja2.meta.find_undeclared_variables` 解析其语法树），再从 `services/ready.py:_VARIABLES` 按变量名取数据。因此新增格式或模板文件只需在 `assets/configs/` 新建模板并按 `<格式大写下划线>_CONFIG_ROOT` 约定命名 `.env` 变量，后端代码无需登记格式、模板或环境变量的对应关系；请求体中的 `train_format` 即以模板目录名为准校验。

模板在沙箱环境（`jinja2.sandbox.ImmutableSandboxedEnvironment`）中渲染：只能取用 `_VARIABLES` 中登记的变量，不能经属性链（如 `cycler.__init__.__globals__`）触及进程环境、文件系统或其他对象；变量名未登记时渲染直接失败，不静默留空。环境变量本身从不作为变量暴露，读取只发生在 `export_dir` / `output_dir` 两条路径上，且键名由**本次请求的格式名**推导（`<格式大写下划线>_CONFIG_ROOT` / `_OUTPUT_ROOT`），模板无法跨格式读取他人配置根。

### diffusion-pipe 差异

- `train.toml` 以项目 `configs/chawenjian/train.toml` 为蓝本：训练/验证/保存与 `[model]`/`[optimizer]`/`[monitoring]` 段固定，`dataset` 指向导出目录内的 `dataset.toml`，`output_dir` 取 `{DIFFUSION_PIPE_OUTPUT_ROOT}/{数据集名}`（训练产物与配置分离，避免各数据集在同一目录下用相同的 `epochN`/`stepN` 子目录名互相覆盖），`[adapter].rank` 随画风/角色模式，`wandb_tracker_name` 由数据集名派生。
- `dataset.toml` 为 diffusion-pipe 结构：顶层 `resolutions = [512, 1024, 1536]` 为该格式的默认训练分辨率（框架读取目录级 `resolutions` 时会先求值该键作为缺省值，故顶层这一项必填）；每个启用且含图的目录按 `num_repeats` 归并成 `[[directory]]` 块——同值的分辨率合并进该块的 `resolutions` 列表（升序），仅当该目录在不同分辨率上分配值不同时才输出多个同 `path` 块（`num_repeats` 是块级标量；框架按块实例化 `DirectoryDataset`，无重复路径限制，缓存目录按分辨率桶区分）。侧栏目录分辨率输入即该块显式声明的 `resolutions`；留空则该块不写 `resolutions`，按顶层默认分辨率训练（此时 `num_repeats` 不再按分辨率均分）。
- `num_repeats` 与 sd-scripts 同源分配：子集级总重复次数取 `max(块数, ceil(目标图片数 / 启用目录图片总数))`，经同一均分函数按分辨率分配（余数优先分给最靠近 1024 的分辨率）。归并成块的规则由模板（`assets/configs/diffusion-pipe/dataset.toml.j2`）决定，代码只给出每目录的逐分辨率重复次数；预览的 `subset_repeats` 两种格式均为逐分辨率列表；重复次数不由用户逐块填写，由「目标图片数 + 各目录分辨率列表」推导。
- 「正则化」开关两种格式均生效：regL 以类提示词（不含触发词）的 caption 作为普通数据块参与同一损失计算（`num_repeats = 1`，分辨率取配置值），即先验保持；diffusion-pipe 无独立 reg loss 权重旋钮，等价于先验保持权重 1.0。正则化数据集列表由 `.env` 的 `REG_DATASETS` 配置（`分辨率:目录绝对路径` 逗号分隔）；开启开关但未配置时报 400。

## 界面

- **顶栏**：格式切换、画风/角色模式切换、生成、复制、`num_repeats`、`resolution`（逗号分隔的多个分辨率，留空则生成单块）、subset 数量、「正则化」开关。
- **左侧栏**：工作目录表格（目录、张数、蒙版完整/无状态）。
- 主区为配置文件预览（标签页随格式变化），内容即导出的配置文本。

## 多分辨率（sd-scripts）

`resolution` 输入多个分辨率（如 `1536,1024,512`）时，按分辨率渲染 `[[datasets]]` 块，块内首行为 `resolution = N`；同一分辨率下再按子集是否携带蒙版目录拆分为蒙版块与普通块，蒙版块在前。留空则保持单块且不带 `resolution` 行。

## 蒙版状态（仅两种）

| 状态 | 含义 |
|------|------|
| 完整 | 该目录中每张图在 `mask/` 下都有对应蒙版 |
| 无 | 尚有缺蒙版的图（含一张都没有） |

蒙版状态为 **完整** 的目录：sd-scripts 格式下其 subset 附带 `conditioning_data_dir`（指向 `mask/` 下同名目录，携带该键的子集独占 `[[datasets]]` 块）；diffusion-pipe 格式下其 `[[directory]]` 块附带 `mask_path`（指向 `mask/` 下同名目录）。其余目录只有图片目录与 `num_repeats`。

## 生成与导出规则

1. `main` 优先，其余按名称排序；无图像目录跳过。
2. 修改蒙版、目录、格式或模式后需重新生成再复制/导出。
3. 导出以编辑后的文本为准，系统不做任何字段改写；全部 `.toml` 文件写入前校验 TOML 语法。
