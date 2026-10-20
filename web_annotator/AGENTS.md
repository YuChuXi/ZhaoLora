# AGENTS.md —— web_annotator 开发标准

本文档是本仓库（`web_annotator/`）面向 AI 与人工协作的统一开发约定。任何修改都应以
本文件为准绳：**先找统一管理器与现有实现，再动手；禁止另起炉灶堆砌补丁**。

## 1. 项目定位与目录

网页图片标注工具（FastAPI 后端 + 原生 JS 前端）。任务根由 `ANNOTATOR_DATA_DIR`
环境变量指定（配置于 `web_annotator/.env`），任务须手动加入清单
（`.annotator_tasks.json`），不会因磁盘子目录自动出现。

```
server.py                      应用入口，装配全局管理器与中间件
routes/                        路由层，只做参数校验/编排，不写业务算法
services/                      业务与基础设施
  core/                        跨业务基础设施：jobs / device / scanner / thumbnailer
  analysis/                    图像分析：wd14 / birefnet / scoring / dedupe / reranker / sam3
  dataset/                     任务运行时：runtime / layout / crop
  state/                       标注状态：annotator / mask / tasks
  online_sources/              在线图源（danbooru / pixiv / bilibili / x / gelbooru）
  tagging/                     标签清洗与统计
  models/                      Pydantic 响应模型，按域拆分
assets/static/js/              前端模块（IIFE 单例，暴露到 window）
assets/static/css/annotator.css 统一样式与控件
assets/templates/              页面 HTML
assets/configs/                训练配置 Jinja2 模板（子目录名即配置导出格式，就绪页导出用）
docs/                          功能说明文档
```

## 2. 统一管理器（后端）

### 2.1 模型生命周期 —— `services/core/device.py`

所有 GPU/CPU 推理模型统一由 `device` 模块管理，禁止在业务模块内自行 `import torch`
加载/卸载。生命周期为 **初始化到内存 → 加载到 GPU ↔ 卸载到内存 → 超时完全释放**。

- 模块内用 `register_model(load, unload)` 登记模型并持有返回的 `Model` 单例（如
  `_AES_MODEL`、`_Q_MODEL`）。
- 获取句柄两种方式，按场景选用：
  - **上下文管理器**（推荐）：`with _MODEL as handle:`，`__enter__` 自动 `acquire`、
    `__exit__` 自动 `release`，适合按分支动态选择模型（见
    `services/analysis/scoring.py:run_scoring`）。
  - **装饰器**：`@_MODEL.auto_release`，以固定形参 `handle` 注入，适合单一模型任务
    （见 `services/analysis/birefnet.py:run_birefnet_mask`）。
- 引用计数归零后模型**驻留内存**供复用，仅空闲超时才彻底卸载。不得手动重复加载。
- 设备由 device 模块统一决定（当前默认 CPU）；调用方**不得**再向下传递 `device` 参数。

### 2.2 长任务编排 —— `services/core/jobs.py`（JobManager）

耗时计算一律提交为长任务，统一交由 JobManager 编排，禁止在请求协程内同步执行推理。

- `submit(kind, group, dataset, run, cancel_fn)` 返回 `job_id`；`group` 相同的任务
  串行，不同组并行。该函数**声明为 async**（内部 `create_task`，须在事件循环中调用）。
- `run(emit)` 在 `asyncio.to_thread` 的工作线程执行，通过 `emit(JobSnapshot)` 线程安全
  回传进度；SSE 由 `event_stream` 输出。
- 进度对象（各模块的 `*Progress` dataclass）须提供 `snapshot() -> JobSnapshot` 供推送。
- 取消采用协作式：`cancel_fn` 置 `cancel_requested` 标志，业务循环内检查后主动退出。

### 2.3 任务运行时 —— `services/dataset/runtime.py`

- `DatasetRegistry`：按名惰性缓存 `Dataset`，`ensure(name)` 打开数据集并提交缩略图
  填充任务（async）。
- `Dataset`：数据集对象，持有 `root` 与派生目录，提供 `work_dirs()` / `work_dir(name)` /
  `select(names)`，并聚合 `thumbnailer` / `state_manager` / `sort_cache`。

### 2.4 请求级依赖 —— `routes/deps.py`

- `DatasetDep`：路由依赖注入的数据集对象（中间件写入 `request.state.runtime`）。
- `RegistryDep`：路由依赖注入的数据集注册表。
- `jobs(request)`：取全局 JobManager。
- `resolve_path(ds, rel)`：在数据集根下安全解析相对路径，失败返回 400。
- `registry(request)`：取数据集注册表。

全局管理器在 `server.py:lifespan` 装配并注册到 `app.state`（`task_store` / `datasets` / `jobs`）。

## 3. 前端统一管理器

- `static/js/api.js`：`API` 单例。数据集绑定一次（`bindDataset` 二次绑定抛错）；所有请求
  经 `API.apiPath(suffix)` 构造数据集作用域路径；`API.request` 统一抛出错误；长任务用
  `API.watchJob(jobId, onEvent)` 订阅 SSE，终态自动 `close`。
- `static/js/store.js`：`Store` 单例。图片列表与选择状态的内存缓存，`toggle` 增量更新并
  通知监听器（`onChange` / `onToggle`），不进行整列表重渲染。
- `static/js/ui.js`：`window.toast(msg)` 全局提示。
- `static/js/nav.js`：功能标签导航与数据集绑定启动。
- 业务页脚本统一为 IIFE：`(function () { 'use strict'; ... })();`，公共能力从上述管理器
  取用，不重复实现。

## 4. 标准控件（模板与样式）

控件均定义于 `static/css/annotator.css`，页面只引用 CSS 类，不得内联重写。

- **模态对话框**：结构固定为
  `.modal > .modal-panel > (.modal-head / .modal-body / .modal-foot)`；外层默认隐藏，
  通过 `.open` / `.show` 显示；`aria-hidden` 控制可达性。见 `tags.html`。
- **状态栏**：`.status-bar` 内含多个 `.status-seg` 分段，段间以分隔线区隔，数值用
  `<b>` 强调，`[hidden]` 隐藏整段。见 `scoring.html` / `crop.html`。
- **Toast 提示**：`<div class="toast" id="toast">` + `window.toast(msg)`。
- **任务进度条**：`.job-progress`（紧凑用 `.compact`）+ `#progress-fill`；`loading` /
  `queued` 时置 `indeterminate` 动画。见 `scoring.html` / `tags.html`。
- **工具栏**：`.toolbar`（紧凑 `.toolbar.compact`），按钮 `.btn`（`.danger` / `.green`）。
- **灯箱**：`.lightbox` 用于大图查看（`scoring.html`）。
- 所有页面以 `<div class="toast" id="toast"></div>` 收尾，并按规定顺序引用脚本。

## 5. 代码风格

### 5.1 通用

- 使用中文进行注释与文档；术语专业统一，禁止黑话与不准确简写（如用"持久化/写入"，
  不用"落盘"）。
- **严禁 `try` 吞异常**：不做空 `except` 掩盖错误，也不做无意义的兜底。异常应在能修复的
  上游正确处理，而不是在下游用复杂逻辑弥补。
- **禁止兜底逻辑**：数据应在上游被完善处理。定位问题时先整理既有代码找根因，禁止
  "加一个保底"式的补丁。
- 相同功能必须整合复用，禁止随处复制相似实现；新增功能优先到 `services/` 与
  `assets/static/js/` 中查找既有实现。
- 不做与旧代码的兼容补丁，除非用户明确要求。

### 5.2 Python

- 文件顶部 `from __future__ import annotations`；类型注解完整。
- **函数必须有规范 docstring**（Google 风格），严格标识：用途 / Args（含类型）/ Returns
  （含类型）/ Callers。Callers 记录全部引用位置：代码调用（`file:function`）与文档/前端
  引用（`docs/*.md`、`static/js/*.js`、`templates/*.html`）。单行简短函数可单行 docstring。
- 响应模型统一用 Pydantic（`services/models/`），禁止随手返回裸 `dict` 结构；请求体用
  Pydantic `BaseModel` 校验。
- 常量集中定义（如分数文件名、标签列表），禁止散落于各处。
- 需要事件循环 / 异步上下文的函数声明为 `async`，让契约在编译期显式化。
- 长任务内部逻辑尽量为同步纯函数，异步编排集中在路由 / 管理器层。
- `torch` 惰性导入（`if TYPE_CHECKING` 仅用于类型注解），避免模块导入即触发重型依赖。
- 禁止新增全局模型缓存（如 `_AES_MODELS` / `_CLIP_CACHE`），统一经 `device.register_model` 登记。

### 5.3 JavaScript

- 一律 `'use strict'`；模块为 IIFE 单例或单例工厂，暴露能力到 `window` / 单一全局名。
- 变量用 `var`，函数命名采用语义化 camelCase；事件回调与状态更新职责分离。
- 通过 `API` / `Store` / `DirPicker` / `window.toast` 取用公共能力，不重复实现既有能力。
- 长任务提交后调用 `API.watchJob` 监听，并在 `done/error/cancelled` 时停止。

## 6. 请求 / 数据流约定

1. 路由 `POST /scoring/start` 等提交任务：构造 `*Progress` → 定义 `run(emit)`（内含
   `on_update` 回调）→ `await jobs(request).submit(...)` → 返回 `job_id`。
2. 进度：工作线程 `emit(snapshot)` → JobManager 线程安全发布 → SSE 推送到前端
   `API.watchJob`。
3. 数据集作用域：路径形如 `/api/d/{dataset}/...`；前端一律经 `API.apiPath` 构造。
4. 文件访问：业务代码不得自行拼接路径访问根目录之外，统一用 `resolve_path` / `resolve_under`。

## 7. 命名与术语

- 统一用专业术语：图像"切帧""蒙版""评分""去重"，避免含糊词（如不用"导航"指代切帧）。
- 模型 / 任务 `kind` 用 snake_case（如 `aesthetic` / `quality` / `birefnet_mask`）。
- 前端模块名与其职责一致，避免冗余前缀（路由处理器不要加与 router 同名前缀）。

## 8. 修改与验证

- 修改后自行验证：后端至少 `python -m py_compile` 与关键路径的轻量运行测试；前端确认
  控件类名与模板结构一致。
- 保持整体统一：新增能力优先复用既有管理器 / 控件，而非新增独立实现。
