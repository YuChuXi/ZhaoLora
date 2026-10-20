# 目录约定

## 任务根与任务清单

启动参数 `--data-dir` 指向任务根，例如 `datasets/`。

任务列表**只**来自任务根下的 `.annotator_tasks.json`，不会扫描子目录自动登记。

```
datasets/                         # 任务根（--data-dir）
  .annotator_tasks.json           # 任务清单
  peanut_whiskers/                # 单个任务目录
    main/                         # 源图；裁剪 / 蒙版输入
    mask/                         # Alpha 蒙版（与 main 同 stem）
    head/ foot/ …                 # 部位裁剪输出
    crops/                        # 未命名选区的存放目录
    .crops/                       # 每图裁剪输出记录
    .thumbnails/                  # 缩略图与色调指纹缓存
    .wd14_meta/                   # WD14 推理缓存（{模型指纹}/{子目录}/{stem}.npy）
    .annotator_settings.json      # 数据集级前端设置（各页输入参数）
    .trash/                       # 回收站
    .annotator_state.json         # 选中态与回收站索引
    .aesthetic_scores.json        # 美学分
    aesthetic_report.html         # 美学报告
```

## 保留目录名

下列名称不会被当作部位输出目录：

`main`、`mask`、`crops`、`aesthetic_report`、`.trash`、`.thumbs`、`.thumbnails`、`__pycache__`

## 路径在界面中的含义

| 模块 | 图像路径相对谁 | 示例 |
|------|----------------|------|
| 筛选、去重、美学、标签、清洗 | 任务目录根 | `main/foo.png` |
| 裁剪、蒙版 | `main/` | `foo.png` |

旁路标注为与图像同 stem 的 `.txt`。删除进回收站时，同 stem 的旁路文件一并移动。

自然语言描述写入图片所在目录的 `captions.json`，键为图片文件名（不含目录）、值为
描述列表。WD14 打标缓存整条输出概率向量到任务目录根下的 `.wd14_meta/`，按图片相对
路径镜像（`main/foo.png` → `.wd14_meta/<模型指纹>/main/foo.npy`）；模型指纹由权重与
标签表的内容哈希共同决定，指纹本身缓存在任务根 `.wd14_model.json` 以免每次启动重读
权重。各页输入参数按页面/组件名持久化为 `.annotator_settings.json`，分两层：全局缺省层在任务根
`datasets/` 下（跨任务通用的参数习惯），数据集层在 `datasets/xxx/` 下（任务特有参数）；读取时按键
合并、数据集键覆盖全局同键，写入按键合并、同键整体替换。数据集层键：`dirs`（目录选择器选中集，
全局层同键作缺省）、`caption`（提示词条目与注入/模式选项）、`crop`（部位默认裁切与禁用部位）；
全局层键：`sort`（筛选页排序方式）、`filter`（`cols` 列数 / `mode` 图站模式 / `show_trash` 回收站
开关）、`paint`（笔刷与底色参数）、`tagging`（WD14 参数）、`tag_stats`（标签统计筛选）、
`tag_batch`（批量编辑参数）、`scoring`（`sort` / `desc` / `filter`）、`dedupe`（算法与阈值）、
`tag_check`（检查参数与 `emb` 向量对话框参数）、`booru_batch`（批量下载参数）、`ready`（`format` /
`variant` / `mode` / `target_images` / `use_reg`）、`video`（B 帧隐藏开关）。浏览器 localStorage 中
旧版设置由首页「导入旧设置」按钮一次性迁移至两层。

## 目录选择器

筛选、去重、美学、标签、清洗等页右上角可多选工作子目录。已选目录随各页输入参数一并
持久化到任务目录根下的 `.annotator_settings.json`（按任务区分）。至少勾选一个目录；清空时回落到 `main`。

裁剪、蒙版、就绪页不使用目录多选（固定处理约定目录）。

## 启动参数（常用）

| 参数 | 说明 |
|------|------|
| `--data-dir` | 任务根（必填） |
| `--host` / `--port` | 监听地址与端口 |
| `--thumbnail-size` | 缩略图长边像素 |

图站代理与认证（`HTTP_PROXY` / `HTTPS_PROXY`、`X_COOKIES`、`PIXIV_REFRESH_TOKEN`、`DANBOORU_LOGIN`、`DANBOORU_API_KEY`）通过 `web_annotator/.env` 配置，启动时统一加载。`X_COOKIES` 为 Netscape 格式 cookies.txt 路径；`PIXIV_REFRESH_TOKEN` 为 pixiv OAuth refresh token（`gallery-dl oauth:pixiv` 获取）。
