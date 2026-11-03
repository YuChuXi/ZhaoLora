# ZhaoLora

面向角色与画风 LoRA 的数据集生产与训练工具链。以本地 Web 应用完成图源采集、筛选、评分、去重、裁剪、蒙版、打标、描述与训练配置导出，并提供 diffusion-pipe 配置与 SDXL LoRA 训练脚本。

仓库不包含数据集、模型权重与训练产物；`datasets/`、`wd14_tagger_model/`、`output/` 等路径由 `.gitignore` 排除。

## 功能

### 数据标注与管理（`web_annotator/`）

- **任务管理**：任务根下的数据集按任务登记，支持进入、复制链接、完成、重新打开。
- **图源采集**：Danbooru、Gelbooru、Pixiv、X、Bilibili、Mihuashi 的搜索与批量下载；X 经 gallery-dl，Bilibili 视频经 BBDown。
- **筛选**：瀑布流浏览、删除与回收站；支持按名称、时间、美学分、质量分、像素排序与语义搜索。
- **评分**：CLIP + 美学头计算美学分，Q-ReAlign 计算质量分；结果写入旁路 JSON，美学评分另生成 HTML 报告。
- **去重**：按相似度阈值与分辨率比对分组，给出建议保留与建议删除项。
- **裁剪**：从任意工作目录选区，吸附训练分辨率桶，输出到部位目录或 `crops/`。
- **蒙版**：Alpha 绘制、模板复用、BiRefNet 自动抠图、SAM3 文本提示分割与蒙版表达式合并。
- **标签**：WD14 ONNX 批量打标、标签频率统计、批量翻译、标签存在性与位置检查。
- **相似度排序**：Qwen3-VL-Reranker 对查询文本与图片逐张评分，支持按阈值批量增删标签。
- **描述**：调用 OpenAI 兼容视觉模型生成自然语言描述，写入 diffusion-pipe 可直接读取的 `captions.json`。
- **视频**：视频扫描、封面瀑布流、按帧类型抽取训练帧。
- **就绪**：按 sd-scripts 或 diffusion-pipe 模板生成训练配置并导出，支持多分辨率、蒙版与正则化数据集。
- **归档**：将已完成任务压缩为 7z（LZMA2 极限压缩），校验后清理工作目录。
- **长任务**：推理与批量操作统一由 JobManager 编排，经 SSE 推送进度，支持协作式取消。

各页面与目录约定的详细说明见 [web_annotator/docs/README.md](web_annotator/docs/README.md)。

### 训练与配置

- **diffusion-pipe**：以 git 子模块引入，配置位于 `configs/`，包含 Krea2、MOSS 等模型与各数据集的训练、评测配置。
- **SDXL LoRA**：`train_text_to_image_lora_sdxl.py` 与 `train_dreambooth_lora_sdxl.py` 来自 HuggingFace diffusers 示例并按本项目需求扩展；`train.sh` 汇总常用参数组合。
- **补丁**：`patches/` 提供 ComfyUI xformers 注意力掩码修正与 diffusion-pipe 学习率探测模块。

### 数据处理脚本

| 脚本 | 用途 |
| --- | --- |
| `prepare_metadata.py` | 由「图片 + 同名 txt」生成 ImageFolder 使用的 `metadata.jsonl`，支持子目录与重复次数配置 |
| `separate_alpha.py` | 将图片 alpha 通道分离为独立灰度 PNG，原图原位转为 RGB |
| `fix_images_jpg_to_png_and_png_alpha.py` | 目录内 JPEG 转 PNG，并为 PNG 补全 alpha 通道 |
| `batch_realcugan_restore_original_size.py` | 批量 Real-CUGAN 超分后缩放回原尺寸，可按蒙版处理透明区域 |
| `visualize_sr_degradation.py` | 可视化反复超分再缩放造成的退化 |
| `analyze_video_frame_quality.py` | 逐帧分析视频质量趋势，输出 CSV 与曲线图 |
| `compute_jpeg_blockiness_hist.py` | 统计数据集 JPEG 块效应分数分布 |
| `compute_jpeg_reencode_mse_distribution.py` | 统计数据集 JPEG 重编码 MSE 分布 |
| `interactive_jpg_score.py` | 交互调整 JPEG 质量并查看块效应得分 |
| `greedy_stitched.py` | 基于图论与尺寸剪枝的九宫格无损拼接 |

## 目录结构

```text
.
├── web_annotator/                 # FastAPI 后端 + 原生 JavaScript 前端
│   ├── routes/                    # 路由与请求校验
│   ├── services/                  # 分析、数据集运行时、在线图源、状态与任务编排
│   ├── assets/                    # 前端脚本、样式、模板与训练配置模板
│   └── docs/                      # 功能与目录约定文档
├── configs/                       # diffusion-pipe 训练与评测配置
├── diffusion-pipe/                # git 子模块：训练框架（GPL-3.0）
├── Real-CUGAN/                    # 超分模型代码（MIT）
├── patches/                       # ComfyUI 与 diffusion-pipe 补丁
├── train_*.py                     # SDXL LoRA 训练脚本
├── prepare_metadata.py            # 数据集元数据生成
└── （其余数据处理脚本）
```

## 环境要求

- Linux，Python 3.12（当前开发环境版本）。
- PyTorch 及其 CUDA 构建，按显卡与驱动选择；CPU 推理可通过 `ANNOTATOR_CPU_ONLY=1` 强制启用。
- 可选：BBDown（Bilibili 视频下载）、OpenAI 兼容视觉模型服务（自然语言描述）。
- diffusion-pipe 需要单独的 Python 环境与 DeepSpeed，安装步骤见 `diffusion-pipe/README.md`。

## 安装

```bash
git clone --recurse-submodules https://github.com/YuChuXi/ZhaoLora.git
cd ZhaoLora

python3 -m venv .venv
.venv/bin/pip install -U pip

# 先安装与 CUDA 匹配的 PyTorch，避免被 requirements 中的 torch 约束替换
.venv/bin/pip install torch torchvision

.venv/bin/pip install -r web_annotator/requirements.txt

# 在线图源与配置模板渲染的必需依赖
.venv/bin/pip install gallery-dl jinja2
```

按需安装可选组件：

```bash
# WD14 打标
.venv/bin/pip install onnxruntime

# 去重页的相似度分布直方图
.venv/bin/pip install matplotlib

# 美学评分使用的 OpenAI CLIP
.venv/bin/pip install git+https://github.com/openai/CLIP.git
```

若克隆时未拉取子模块：

```bash
git submodule update --init --recursive
```

## 配置

复制配置模板并填写本地路径与凭证：

```bash
cp web_annotator/.env.example web_annotator/.env
```

`.env` 中的键按用途分组：

| 键 | 说明 |
| --- | --- |
| `ANNOTATOR_DATA_DIR` | 任务根目录，例如 `datasets` |
| `ANNOTATOR_CPU_ONLY` | 设为 `1` 时所有推理模型仅使用 CPU |
| `ANNOTATOR_THUMBNAIL_SIZE` | 缩略图长边像素 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 图源访问与下载使用的代理 |
| `X_COOKIES` / `PIXIV_COOKIES` | Netscape 格式 cookies 文件路径 |
| `DANBOORU_LOGIN` / `DANBOORU_API_KEY` | Danbooru 认证 |
| `GELBOORU_USER_ID` / `GELBOORU_API_KEY` | Gelbooru 认证与标签补全 |
| `BBDOWN_PATH` | BBDown 可执行文件路径；留空时从 `PATH` 查找 |
| `CAPTION_OPENAI_BASE_URL` / `CAPTION_OPENAI_MODEL` | 描述服务的 OpenAI 兼容根地址（含 `/v1`）与视觉模型名 |
| `WD14_MODEL` | WD14 ONNX 权重路径，标签表取同名 `.csv` |
| `SAM3_MODEL` | SAM3 权重目录或 HuggingFace repo id |
| `BIREFNET_WEIGHT` | BiRefNet 权重路径 |
| `AESTHETIC_CLIP_MODEL` / `AESTHETIC_HEAD_CKPT` | 美学评分骨干与分类头 |
| `QREALIGN_MODEL` / `RERANKER_MODEL` | 质量评分与重排序模型 |
| `SD_SCRIPTS_CONFIG_ROOT` | sd-scripts 配置导出根目录 |
| `DIFFUSION_PIPE_CONFIG_ROOT` | diffusion-pipe 配置导出根目录 |
| `DIFFUSION_PIPE_OUTPUT_ROOT` | diffusion-pipe 训练产物根目录 |
| `COMFYUI_MODELS_ROOT` | 训练配置中底模、VAE、文本编码器的根目录 |
| `REG_DATASETS` | 正则化数据集列表，格式为 `分辨率:目录` |

完整键与默认值见 `web_annotator/.env.example`。

## 启动

```bash
web_annotator/start.sh
```

或从仓库根目录直接启动：

```bash
.venv/bin/uvicorn server:app --app-dir web_annotator --host 0.0.0.0 --port 8765
```

默认地址为 `http://127.0.0.1:8765`。`start.sh` 自动向上查找 `.venv` 并读取 `web_annotator/.env`。

## 数据集目录约定

```text
datasets/                          # 任务根
  .annotator_tasks.json            # 任务清单
  <task>/
    main/                          # 源图；裁剪与蒙版输入
      captions.json                # 该目录图片到自然语言描述的映射
    mask/                          # Alpha 蒙版，与源图同 stem
    head/ foot/ ...                # 部位裁剪输出
    crops/                         # 未命名选区的输出
    video/                         # 视频与抽帧
    .aesthetic_scores.json         # 美学分
    .quality_scores.json           # 质量分
    .wd14_meta/                    # WD14 推理缓存
    .annotator_settings.json       # 数据集级前端设置
    .trash/                        # 回收站
```

筛选、去重、评分、标签等页面以任务目录根为基准；裁剪与蒙版页以 `main/` 为基准。旁路标注为与图片同 stem 的 `.txt`。

## 使用流程

1. 在首页添加任务，进入任务。
2. 在筛选页采集或导入图片，按评分与语义搜索挑选样本。
3. 在去重页处理相似图，在裁剪页输出部位图，在蒙版页绘制或自动生成 Alpha。
4. 在标签页生成或清洗标签，在标签检查页按位置补齐标签，在描述页生成 `captions.json`。
5. 在就绪页选择 sd-scripts 或 diffusion-pipe 格式，生成并导出训练配置。
6. 按下方训练命令启动训练；完成后可将任务归档。

## 训练

### diffusion-pipe

先按 `diffusion-pipe/README.md` 创建环境并安装依赖，然后修改 `configs/` 下对应配置中的模型、数据集与输出路径：

```bash
cd diffusion-pipe
deepspeed --num_gpus=1 train.py --deepspeed --config ../configs/chawenjian/train.toml
```

`dataset.toml` 中的 `[[directory]]` 支持 `mask_path`、`num_repeats` 与目录级 `resolutions`；`captions.json` 可直接作为目录级描述来源。

### SDXL LoRA

```bash
.venv/bin/python prepare_metadata.py --data-dir datasets/<task>

.venv/bin/python train_text_to_image_lora_sdxl.py \
  --pretrained_model_name_or_path=/path/to/model.safetensors \
  --train_data_dir=datasets/<task> \
  --output_dir=output/<run> \
  --resolution=1024 \
  --rank=32 \
  --lora_alpha=32 \
  --max_train_steps=1000 \
  --mixed_precision=fp16
```

`train.sh` 保留了可用的完整参数组合，使用前需将模型路径、数据目录与输出目录改为本机路径。`train_dreambooth_lora_sdxl.py` 提供 DreamBooth 方式的正则化训练。

## 许可证

本项目原创代码（`web_annotator/`、`configs/`、根目录脚本）以 **GNU General Public License v2.0 或更高版本**发布，SPDX 标识为 `GPL-2.0-or-later`，完整文本见 [LICENSE](LICENSE)。Copyright (C) 2026 YuChuXi。

选择该协议的依据：`web_annotator` 在进程内导入 **gallery-dl**，其许可证为 GPL-2.0-only。原创代码采用 `or-later` 授予下游在 GPLv3 下组合 Apache-2.0 依赖的空间；但包含 gallery-dl 的整体分发仍须按 GPL-2.0 进行。若需采用更宽松的许可证，应先将 gallery-dl 改为独立进程调用或替换为许可证兼容的实现。

仓库包含或引用的其他组件保留各自许可证，不适用根目录 `LICENSE`：

| 组件 | 位置 | 许可证 |
| --- | --- | --- |
| diffusion-pipe | `diffusion-pipe/`（git 子模块） | GPL-3.0 |
| ComfyUI | `diffusion-pipe/submodules/ComfyUI/` | GPL-3.0 |
| Cosmos | `diffusion-pipe/submodules/Cosmos/` | Apache-2.0 |
| HiDream | `diffusion-pipe/submodules/HiDream/` | MIT |
| HunyuanVideo / HunyuanImage-2.1 | `diffusion-pipe/submodules/` | Tencent Hunyuan Community License |
| Real-CUGAN | `Real-CUGAN/` | MIT |
| diffusers SDXL 训练示例 | `train_text_to_image_lora_sdxl.py`、`train_dreambooth_lora_sdxl.py` | Apache-2.0 |
| ComfyUI / diffusion-pipe 补丁 | `patches/` | GPL-3.0 |
| gallery-dl | Python 依赖 | GPL-2.0-only |
| py7zr | Python 依赖 | LGPL-2.1-or-later |
| BBDown | 外部可执行文件 | MIT |

其余 Python 依赖均为宽松许可证，包括 MIT（FastAPI、pydantic、curl_cffi、tomlkit、onnxruntime）、BSD-3-Clause（uvicorn、httpx、PyAV、jinja2、python-dotenv、NumPy、PyTorch、torchvision）、MIT-CMU（Pillow）、Apache-2.0（OpenCV、transformers、diffusers、accelerate、peft、datasets、safetensors、deepspeed）、Apache-2.0 WITH LLVM-exception（wasmtime）与 PSF 风格许可（matplotlib）。

模型权重不随仓库分发，使用时须遵守各模型自身的许可证：

| 模型 | 用途 | 许可证 |
| --- | --- | --- |
| `SmilingWolf/wd-vit-large-tagger-v3` | WD14 打标 | Apache-2.0 |
| `ZhengPeng7/BiRefNet` | 背景移除蒙版 | MIT |
| `facebook/sam3` | 文本提示概念分割 | Meta SAM License（需申请访问） |
| `q-future/Q-ReAlign-Lite-4B` | 质量评分 | Apache-2.0 |
| `Qwen/Qwen3-VL-Reranker-2B` | 相似度排序与重排序 | Apache-2.0 |
| OpenAI CLIP ViT-L/14 | 美学评分骨干 | MIT |
| `purplesmartai/aesthetic-classifier` | 美学评分头 | 模型卡未声明许可证，使用前须自行确认授权 |
