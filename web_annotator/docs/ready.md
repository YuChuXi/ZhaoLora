# 就绪

根据当前任务工作目录生成训练用配置文本，供复制到训练配置或一键导出。

## 配置格式

顶栏格式切换段决定导出的配置文件集合与导出根目录：

| 格式 | 文件集合 | 导出根目录（`.env`） |
|------|----------|----------------------|
| sd-scripts | `lora.toml` / `dataset.toml` / `prompt.txt` 三件套 | `SD_SCRIPTS_CONFIG_ROOT` |
| diffusion-pipe | `train.toml` / `dataset.toml` 两件套 | `DIFFUSION_PIPE_CONFIG_ROOT` |

导出写入 `{格式对应配置根}/{数据集名}/`；相对路径按 `web_annotator/` 目录解析。目标目录已存在配置时需确认覆盖。切换格式后自动重新生成，各文本区编辑内容随重建丢弃。

配置文本由 `web_annotator/assets/configs/{sd-scripts,diffusion-pipe}/` 下的 Jinja2 模板渲染（`services/ready.py:render_template`），代码仅装配数据（路径、重复次数、分组与正则化数据段）。

### diffusion-pipe 差异

- `train.toml` 以项目 `configs/chawenjian/train.toml` 为蓝本：训练/验证/保存与 `[model]`/`[optimizer]`/`[monitoring]` 段固定，`output_dir` 指向导出目录内 `output/`，`dataset` 指向同目录 `dataset.toml`，`[adapter].rank` 随画风/角色模式，`wandb_tracker_name` 由数据集名派生。
- `dataset.toml` 为 diffusion-pipe 结构：顶层 `resolutions` 固定 `[512, 1024, 1536]`，每个启用且含图的目录一个 `[[directory]]` 块；侧栏目录分辨率输入映射为该目录的目录级 `resolutions` 覆盖（留空则继承顶层）。
- `num_repeats` 为目录级统一值 `max(1, ceil(目标图片数 / 启用目录图片总数))`，与 sd-scripts 的分辨率块拆分口径不同。
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
3. 导出以编辑后的文本为准；仅 sd-scripts 格式下 `lora.toml` 的 `output_name` / `sample_prompts` / `dataset_config` 三项路径由系统按真实导出目录覆写；全部 `.toml` 文件写入前校验 TOML 语法。
