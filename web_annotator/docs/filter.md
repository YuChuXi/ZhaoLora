# 筛选

瀑布流浏览与挑选样本；支持本地目录与 Gelbooru / Danbooru / Bilibili。

## 本地模式

1. 「来源」选 **本地**，右上角勾选工作目录。
2. 单击选中 / 取消；顶栏 **已收录 / 已选择 / 回收站**。
3. **删除** 将选中项移入回收站；**恢复** 可整库或按选中恢复。
4. 右键可找色调相似图。

### 快捷键（本地）

| 键 | 行为 |
|----|------|
| Delete | 删除选中 |
| A | 全选 |
| D | 取消全选 |

## 远程模式（Gelbooru / Danbooru / Bilibili）

1. 「来源」选图站，输入标签，设置条件与页码后 **搜索**。
2. 左键触发 **下载** 并写入当前工作子目录；完成后状态为 **已保存**。
3. 顶栏 **已收录**（工作目录磁盘张数）· **已加载**（当前搜索结果）。点选即写入，无「已选择」。
4. 「条件」角标统计已生效项（评级、分数/宽高/ID、用户、附加元标签；日期仅 Danbooru）；悬停可看摘要。
5. 对已保存条目可 **移除**（从工作目录删去对应文件）；移除后「已收录」会刷新。
6. 更改列数后会尽量回到原先浏览的页位。

远程产物（每个帖子）：图像、旁路 `.txt`、以及对应的元数据目录。

### Bilibili 模式

1. 「来源」选 **Bilibili**，输入 **UID**（必填）后搜索；可选按类型（图片/视频/转发）、关键词与发布时间区间筛选。
2. 一条图文动态的多张原图会展开为多张独立卡片；转发动态取原文配图，视频动态用封面。
3. 卡片预览在右侧以自然语言段落展示动态正文（而非标签芯片）。
4. 文件名形如 `bi{动态ID}_{序号}.png`，旁路 `.txt` 写入动态正文；元数据存于 `.bilibili_meta`。
5. 首次搜索会拉取该用户全部动态并缓存，之后分页读取缓存；换筛选条件会重新拉取。

## 新增来源

来源差异前后端均已收敛为注册表，新增来源只需在固定位置补一项：

后端（`services/online_sources/`）：
1. 若用 gallery-dl 驱动（X / pixiv 等），`xxx.py` 继承 `framework/gallerydl.py` 的 `GalleryDLSource`，并实现 `_search_key` / `_search_url` / `_detail_url` / `_split_pid` / `_post_from_item` / `_caption_from_item` / `_auth_overrides` 等钩子；`search` / `download` / `fetch_meta` 均由基类提供。
2. 否则继承 `SourceClient`，自行实现 `search` / `download` / `fetch_meta`；查询串特殊时覆盖 `parse_query`。
3. `__init__.py` 中 `register_source(XXXSource())`。
4. 若图床需防盗链代理，在 `routes/booru.py` 加对应图片代理端点。

前端（`templates/index.html` + `static/js/booru_filter.js`）：
1. `index.html` 加一个 `<option>` 与对应 `toolbar-xxx` 工具栏（控件 id 与注册表 `stateKeys` 一致）。
2. `booru_filter.js` 的 `_SOURCES` 注册表加一项：`toolbarId` / `searchBtn` / `batchBtn` / `batchDialog` / `stemPrefix` / `nameLabel` / `stateKeys` / `buildRequest` / `thumb` / `sample` / `pageUrl`，预览渲染特殊时加 `renderPreviewTags`。
3. 业务主干（搜索、分页、下载、预览、工具栏切换）无需改动。

## 代理

仅 Gelbooru 缩略图/预览经 `/booru/thumb|sample`（防盗链 + 磁盘缓存）。B 站图片经 `/booru/image` 代理（带 B 站 Referer 规避图床防盗链）。Danbooru 直链。搜索出口用 `--booru-proxy`。

## 批量下载

工具栏 **批量下载**：服务端按当前搜索条件向后翻页下载，跳过已保存，直到达到指定张数。可设连接数（1–8）与限速（次/秒，0=不限）；运行中可改。进度在顶栏与对话框；**刷新页面会自动重连**进行中的任务。点「停止」可取消。Gelbooru 遇 HTTP 429 自动退避重试。
