# 美学

路径：`/aesthetic`

对所选子目录中的图像计算美学分，写入数据集根，并可选生成 HTML 报告。

## 模型与依赖

- 实现脚本：仓库根目录 `aesthetic_score_dataset_pony.py`
- 模型：CLIP ViT-L/14 + purplesmartai `aesthetic-classifier`
- 原始分范围约 `[0, 1]`
- Pony 档位映射：`score_N`，其中 `N = int(clamp(raw, 0, 0.99) * 10)`，即 `0…9`

## 输入

| 项 | 说明 |
|----|------|
| 目录 | 右上角多选；`GET /api/aesthetic/dirs` 列出含图像的一级子目录 |
| 设备 | UI 可选 CPU / CUDA；CUDA 不可用时禁用对应选项 |

## 输出（文件系统）

| 路径 | 内容 |
|------|------|
| `<root>/.aesthetic_scores.json` | `{ "main/a.png": 0.734772, … }`，覆盖写入本次结果中成功项 |
| `<root>/aesthetic_report.html` | 有结果时生成 / 覆盖 |
| `<root>/.thumbnails/` | 报告卡片按需引用 `/thumb/{hash}.avif` |

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/aesthetic/dirs` | 目录列表、CUDA 是否可用、报告是否存在 |
| GET | `/api/aesthetic/status` | 进度快照（phase、done/total、logs、report 信息） |
| GET | `/api/aesthetic/report` | 改写后的报告 HTML（卡片缩略图；灯箱原图走 `/api/image`） |
| POST | `/api/aesthetic/start` | body：`{ dirs, device, write_report }`；已有任务运行中返回 409 |
| POST | `/api/aesthetic/cancel` | 请求取消；若已有部分结果会先保存分数 |

## 行为细节

1. 扫描所选目录中的图像，逐张推理。
2. 成功：记 `raw` 与内部 `pony` 档位；失败：计入 `failed`，不写该路径分数。
3. 空目录：状态 `done`，提示无可处理图片。
4. 前端约 300ms 轮询状态；报告以 iframe 嵌入。
5. 单进程同时仅允许一个美学任务。

## 与其它模块的关系

- **筛选**：列表展示美学分，可按美学排序（无分视为 -1）。
- **清洗**：可选读取 `.aesthetic_scores.json`，在 caption 头部写入 `score_N`。

## 快捷键

无。
