# 筛选

路径：`/filter`

瀑布流浏览所选目录图像：选中、删除进回收站、恢复、按色调找相似图。

## 挂载 Gelbooru / Danbooru

筛选页「来源」可切换为远程图站：

1. 选择 **Gelbooru** 或 **Danbooru**
2. 输入搜索 tag（空格分隔，语法与站内一致）
3. **搜索** / **加载更多**：瀑布流展示预览图（经本机 `/api/booru/thumb` 代理）
4. **点击选中即自动下载**到右上角当前目录（默认取多选中的第一个，一般为 `main`）

### 下载产物

写入 `<root>/<dir>/`：

| 文件 | 说明 |
|------|------|
| `{id}.jpg/png/…` | 原图 |
| `{id}.txt` | 旁路标签（逗号分隔） |
| `.gelbooru_meta/{id}.json` 或 `.danbooru_meta/{id}.json` | 帖子元数据 |

已存在同 id 图像与 txt 时跳过下载（仍记为已完成）。

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/booru/search` | `{ source, tags, page, limit }` |
| GET | `/api/booru/thumb?source=&id=` | 预览图代理 |
| POST | `/api/booru/download` | `{ source, id, dir, overwrite? }` |

### 代理

启动参数 `--booru-proxy`（默认 `http://127.0.0.1:7890`）；设为空字符串则直连。

### 注意

- 远程模式下本地删除 / 回收站 / 相似图快捷键停用。
- Danbooru 部分帖子无 `file_url`（登录墙）会搜索不到或下载失败。
- Gelbooru 单页通常最多约 100 条。

---

## 目录范围

- 右上角多选工作目录（排除 `mask` 等非工作目录）。
- 列表路径相对数据集根，如 `main/foo.png`。
- 「显示回收站」开启时，合并 `.trash/` 中对应项。

## 列表数据

`GET /api/images?dirs=&show_trash=` 返回每项：

- `path` / `stem` / `width` / `height`
- `selected` / `trashed`
- `thumbnail_hash`（`/thumb/{hash}.avif`）
- `aesthetic`（来自 `.aesthetic_scores.json`，可能为空）

尺寸带 mtime 缓存；列表接口**不再**同步生成缩略图。缩略图加载失败时前端回退 `/api/image`。

## 排序

`localStorage` 键：`annotator_sort`

| 值 | 行为 |
|----|------|
| `name` | 按路径名（默认） |
| `aesthetic` | 美学分降序；无分按 -1 |
| `pixels` | `width * height` |

## 选中与删除

| 操作 | API / 行为 |
|------|------------|
| 单击卡片 | `POST /api/toggle`，切换选中 |
| 全选 | `POST /api/select-all`，仅当前所选目录中未删图 |
| 取消全选 | `POST /api/deselect-all`，清空**全局** selected |
| 反选 | 对当前可见项逐张 toggle |
| 删除 | `POST /api/apply`：将**全局** selected 移入 `.trash/`，更新 state；清空内存色调索引 |
| 恢复 | `POST /api/restore`：`paths` 为空则恢复全部 trashed；否则恢复指定项 |

删除时：图像 + 同 stem 的非图片旁路文件一并移入 `.trash/`，保留相对路径结构。

## 色调相似

- 触发：卡片**右键**打开浮层（非悬停）。
- `GET /api/images/similar?path=&limit=`（前端常用 limit=12）。
- 候选范围：与查询图**同目录**、未在回收站。
- 指纹可缓存于 `.thumbnails/color_fp_v2.npz`。
- 点击相似项：`Masonry.jumpTo` 滚动定位。
- 关闭：Esc、点击外部、滚动或缩放页面。

## 缩略图懒加载

瀑布流使用 `IntersectionObserver`：仅视口附近约 400px 内的图片设置 `src`，避免局域网下原生 `loading=lazy` 预取过猛。

## 列数

滑条 `0` = 按容器宽度自动（约 2–6 列）；`1–8` = 固定列数。

## 快捷键

| 键 | 行为 |
|----|------|
| Ctrl/Cmd+A | 全选 |
| Ctrl/Cmd+Shift+A | 取消全选 |
| Ctrl/Cmd+I | 反选 |
| Delete | 确认后删除选中 |
| Ctrl/Cmd+Z | 恢复（确定=全部回收站；取消对话框=仅恢复当前选中的回收站项） |

## 边界情况

- `apply` 作用于全局 selected，不限于当前目录勾选。
- 统计「总计」不含回收站内文件。
- 删除 / 恢复后色调索引清空，下次相似查询重新构建。
