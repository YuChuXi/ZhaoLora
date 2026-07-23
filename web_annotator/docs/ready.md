# 就绪

路径：`/ready`

根据当前数据集工作目录生成训练用 `[[datasets.subsets]]` TOML **文本**，供复制到训练配置。本页**不写磁盘**。

## 目录范围

- 目录选择器**隐藏**。
- 请求固定 `dirs: null`，表示包含**全部**含图像的工作目录。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/ready/dirs` | 可导出目录列表 |
| POST | `/api/ready/toml` | `{ dirs?: string[] \| null, num_repeats: 1..100 }` → TOML 文本 |

## 生成规则

1. 目录顺序：`main` 优先，其余按名称排序。
2. 无图像的目录跳过。
3. 每个目录一条 `[[datasets.subsets]]`：
   - `image_dir`：该目录绝对或约定路径（以实现为准）
   - `num_repeats`：UI 输入（默认常见为 1）
4. **仅 `main`**：若 `mask/` 存在且其中有图，则附加 `conditioning_data_dir` 指向蒙版目录。
5. 其它部位目录只有 `image_dir` + `num_repeats`，不附带 conditioning。

## UI

1. 调整 `num_repeats`
2. 生成预览
3. 一键复制到剪贴板

## 快捷键

无。

## 注意

- 输出为配置片段，需人工粘贴进训练 TOML。
- 修改蒙版或目录结构后应重新生成再复制。
