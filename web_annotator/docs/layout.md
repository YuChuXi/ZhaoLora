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
    .wd14_meta/                   # WD14 打标缓存（按图片相对路径镜像）
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
描述列表；WD14 打标的标签缓存写入任务目录根下的 `.wd14_meta/`，按图片相对路径镜像
（`main/foo.png` → `.wd14_meta/main/foo.json`）。

## 目录选择器

筛选、去重、美学、标签、清洗等页右上角可多选工作子目录。状态保存在浏览器本地（按任务名区分）。至少勾选一个目录；清空时回落到 `main`。

裁剪、蒙版、就绪页不使用目录多选（固定处理约定目录）。

## 启动参数（常用）

| 参数 | 说明 |
|------|------|
| `--data-dir` | 任务根（必填） |
| `--host` / `--port` | 监听地址与端口 |
| `--thumbnail-size` | 缩略图长边像素 |

图站代理与认证（`HTTP_PROXY` / `HTTPS_PROXY`、`X_COOKIES`、`PIXIV_REFRESH_TOKEN`、`DANBOORU_LOGIN`、`DANBOORU_API_KEY`）通过 `web_annotator/.env` 配置，启动时统一加载。`X_COOKIES` 为 Netscape 格式 cookies.txt 路径；`PIXIV_REFRESH_TOKEN` 为 pixiv OAuth refresh token（`gallery-dl oauth:pixiv` 获取）。
