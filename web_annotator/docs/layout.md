# 目录约定与元数据

## 目录树

`--data-dir datasets/xxx`（或 `datasets/xxx/main`）解析后：

```
datasets/xxx/                 # root（数据集根）
  main/                       # 源图；裁剪 / 蒙版输入
  mask/                       # Alpha 蒙版（与 main 同 stem）
  head/ foot/ …               # 部位裁剪输出（目录名 = 选区名）
  crops/                      # 未命名选区兜底
  .thumbnails/                # AVIF 缩略图与色调指纹缓存
  .trash/                     # 回收站（保留相对路径结构）
  .annotator_state.json       # 选中态与回收站索引
  .aesthetic_scores.json      # 相对路径 → 原始美学分 [0,1]
  aesthetic_report.html       # 美学 HTML 报告
  .guo582_translation_cache.json  # 可选；标签中文翻译缓存
```

## 保留目录名

下列名称**不会**被当作部位输出目录：

`main`、`mask`、`crops`、`aesthetic_report`、`.trash`、`.thumbs`、`.thumbnails`、`__pycache__`

以 `.` 开头的目录一般视为元数据或隐藏目录。

## 路径规则

| 模块 | 路径基准 | 示例 |
|------|----------|------|
| 筛选、去重、美学、标签、清洗 | 数据集根 | `main/foo.png`、`foot/bar.png` |
| 裁剪、蒙版 | `main/` | `foo.png` |

旁路标注为与图像 **同 stem** 的 `.txt`（逗号分隔标签）。删除进回收站时，同 stem 的非图片旁路文件（如 `.txt`）一并移动。

## 元数据文件

### `.annotator_state.json`

```json
{
  "selected": { "main/a.png": 1710000000 },
  "trashed": { "main/b.png": 1710000001 }
}
```

由筛选、去重的删除 / 恢复写入。时间戳为操作时刻。

### `.aesthetic_scores.json`

键为相对根的图像路径，值为原始美学分（浮点，约 6 位小数）。清洗模块可将其映射为 `score_0`…`score_9`。

### `.thumbnails/`

- 缩略图：`{hash}.avif`，hash 含相对路径与长边尺寸。
- 色调指纹缓存：`color_fp_v2.npz`（相似图检索用）。

### `.trash/`

删除后的文件按原相对路径存放，例如 `main/foo.png` → `.trash/main/foo.png`。

## 启动参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--data-dir` | （必填） | 数据集根或 `main` 路径 |
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8765` | 监听端口 |
| `--thumbnail-size` | `2560` | 缩略图长边像素 |
| `--skip-thumbnails` | 关 | 跳过启动时缩略图预生成 |
| `--mask-dir` | `<root>/mask` | Alpha 蒙版目录 |
| `--default-crop` | 空 | 裁剪默认匹配，如 `1536x1536@1.0` |

## 目录选择器

- 组件：`dir_picker.js`；状态键 `annotator_work_dirs`。
- 至少勾选一个目录；清空时回落到 `main`。
- **不显示**选择器的页面：首页、裁剪、蒙版、就绪。
