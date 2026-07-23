# 清洗

路径：`/tag-clean`

对所选目录中的旁路 `*.txt` 做规范化与重组（**不递归**子目录）。可先预览再写入。

## API

`POST /api/tags/clean`

| 字段 | 说明 |
|------|------|
| `dirs` | 子目录名 |
| `drop` | 整词精确删除列表 |
| `drop_contains` | 子串删除列表 |
| `promote` | 提升至头部（仅重排已存在项） |
| `prepend` | 头部前置（可新增） |
| `inject_pony_score` | 是否写入 Pony 美学档位 |
| `promote_separator` | 是否以 `|||` 分隔头部与正文 |
| `dry_run` | `true` 仅预览 |
| `sample_limit` | 返回对比样本数（前端常用 60，上限 200） |

写入前前端 `confirm`。`dry_run=false` 时覆盖原文件，末尾补 `\n`。

## 流水线顺序（严格）

对每条 caption：

1. **HTML 实体还原**（`html.unescape`）
2. **拆分**：逗号、`|||`、换行 → tag 列表
3. **下划线规范化为空格**（每个 tag；美学档位写入时除外）
4. **删除**
   - 用户整词：忽略大小写与下划线差异的精确匹配
   - 用户子串：小写包含匹配
   - **内置**：tag 文本（小写）含 `commentary` 或 `request` 则删除
5. **提升（promote）**  
   将列表中已出现的项移到头部：先按规范化键精确匹配，未命中再子串匹配；保序。
6. **Pony 美学档位**（`inject_pony_score=true` 时）
   - 去掉已有 `score_0`…`score_9`（含空格形式 `score N`）
   - 用同 stem 旁路图像路径查 `<root>/.aesthetic_scores.json`
   - 映射为 `score_N`（**保留下划线**），插入头部
   - 无图或无分数：该文件不插入，计入 `pony_missing`
7. **头部前置（prepend）**  
   规范化后插入；正文中与前置同键的项去重移除。
8. **可选 `|||` 分隔**  
   头部块 = 美学档位 + 前置 + 被提升项；其后为其余标签。

最终头部顺序示例：

```text
score_9, @chen bin, 1girl, solo ||| long hair, …
```

（未开分隔时无 `|||`，仍为同一逗号序列。）

## UI 选项

| 开关 | 含义 |
|------|------|
| 写入 Pony 美学档位 | 执行流水线第 6 步 |
| 以 \`\|\|\|\` 分隔头部与正文 | 执行流水线第 8 步 |

表单四格：整词删除、子串删除、提升至头部、头部前置。

## 返回摘要字段

- `changed` / `unchanged` / `file_count` / `failed`
- `inject_pony_score`、`pony_injected`、`pony_missing`
- `samples[]`：`before` / `after` 截断对比

## 边界

- 仅处理目录内直接的 `*.txt`，不进入子文件夹。
- 美学分键必须与旁路图相对根路径一致（如 `foot/a.png` 对应 `foot/a.txt`）。
- 与 CLI `clean_dataset_tags.py` 共用 `services/tag_clean.py`（CLI 侧美学注入需自行传参，以网页开关为准）。
