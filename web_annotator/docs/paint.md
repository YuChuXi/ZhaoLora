# 蒙版

路径：`/paint`

在 `main/` 源图上绘制 Alpha 通道，写入 `mask/` 目录。

## 目录范围

- 目录选择器**隐藏**；仅处理 `main/`。
- 蒙版路径：默认 `<root>/mask/{stem}.png`（可用 `--mask-dir` 覆盖）。
- 列表优先**尚无蒙版**的图像。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/paint/images` | 列表 + `has_mask` |
| GET | `/api/paint/image?path=` | RGB 预览（AVIF） |
| GET | `/api/paint/mask?path=` | 灰度 PNG；不存在则 404 |
| POST | `/api/paint/upload-mask?path=&format=` | body 为 PNG；`format=alpha_bw`（默认）或 `rgba` |
| GET/POST | `/api/paint/config` | `default_alpha`（进程内内存，默认 255，**不落盘**） |

尺寸与源图不一致的 mask 视为无效（当作无蒙版）。

## 绘制行为

| 操作 | 效果 |
|------|------|
| 左键 | 默认擦除（alpha=0）；可关闭「左键擦除」改为涂 255 |
| 右键 | 涂不透明 255 |
| 笔刷半径 | `sqrt(W×H) × pct/100`；pct 默认 3%，滚轮调节约 0.5%–20% |
| 全透明 / 全不透明 | 整幅重置 |
| 恢复默认 alpha | 按 `default_alpha` 填充 |

## 保存策略

- **上一张 / 下一张**：自动保存（`doSave(false)`）  
  - 已有 mask 且本次未修改 → 跳过写入  
  - **尚无 mask**：即使未涂改也会写出一份默认 alpha 蒙版  
- **跳过**：不保存，直接切图  
- **S / 保存按钮**：显式保存当前

## 快捷键

| 键 | 行为 |
|----|------|
| ← | 上一张（含自动保存逻辑） |
| → | 下一张（含自动保存逻辑） |
| S | 保存 |
| X | 跳过（不保存） |

## 与训练的关系

就绪导出时，仅当处理 **main** 且 `mask/` 存在且含图，才会在 TOML 中附加 `conditioning_data_dir`。
