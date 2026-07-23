# 标签

路径：`/tags`  
（旧入口 `/tagging` 重定向至本页。）

本页包含：**频率统计**与对话框 **自动生成（WD14）**。

---

## 一、频率统计

### 目录

`GET /api/tags/dirs`：含旁路 `.txt` 的一级子目录；`main` 置顶。

### 请求

`POST /api/tags/stats`

| 字段 | 默认 | 说明 |
|------|------|------|
| `dirs` | — | 子目录名列表 |
| `lower` | `true` | 忽略大小写合并 |
| `underscores` | `true` | 下划线与空格视为同一键 |
| `html_unescape` | `true` | HTML 实体还原 |
| `per_sample` | `true` | 每文件内 tag 先去重再计频；占比 = count / 样本数 |
| `min_count` | `1` | 最低出现次数过滤 |

当 `per_sample=false`：按出现总次数计频；占比 = count / 全部出现次数之和。

### 中文列

合并查找 `.guo582_translation_cache.json`（本数据集或同级数据集中的缓存）。无翻译显示为占位。

### UI

- 勾选行 → 导出框汇总英文 tag（逗号分隔）
- 支持全选 / 清空 / 导出英文 / 导出中文 / 复制
- 切换目录后自动重新统计

无专用快捷键。

---

## 二、自动生成（WD14）

入口：工具栏 **自动生成** → 模态对话框。任务在后台运行，关闭对话框不中断。

### 模型路径

```
wd14_tagger_model/SmilingWolf_wd-vit-large-tagger-v3/
  model.onnx
  selected_tags.csv
```

模型缺失时对话框提示未就绪，禁止开始。

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/tagging/dirs` | 目录、模型是否就绪、默认阈值 |
| GET | `/api/tagging/status` | 进度、日志、ok/skip/fail |
| POST | `/api/tagging/start` | 启动任务 |
| POST | `/api/tagging/cancel` | 请求取消 |

### 启动参数

| 字段 | 前端默认 | 说明 |
|------|----------|------|
| `dirs` | 当前目录选择 | 要打标的子目录 |
| `threshold` | `0.1` | 置信度下限 |
| `disable_orig_tags` | `true` | 忽略图像已有标签，不以原 caption 合并 |
| `force` | `true` | 已存在 `.txt` 时仍覆盖；`false` 则跳过 |

### 写出规则

- 对目录内每张图写旁路 `{stem}.txt`
- 标签按置信度排序；过滤 rating 类标签
- 逗号分隔；与 `guo582_label_tk` 批量无 GUI 逻辑对齐

### UI 细节

- Esc 或点遮罩关闭对话框（任务可继续）
- 运行中禁用目录选择器与部分参数控件
- 约 300ms 轮询；顶栏按钮在运行中显示「自动生成…」
- 同时仅允许一个打标任务

### 与清洗的关系

打标产出的 `.txt` 通常再经 [清洗](tag-clean.md) 做规范化、前置触发词与 Pony 美学档位写入。
