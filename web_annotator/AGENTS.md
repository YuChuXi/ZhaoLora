# AGENTS.md —— web_annotator 开发标准

本文档是本仓库（`web_annotator/`）面向 AI 与人工协作的统一开发约定。任何修改都应以
本文件为准绳：**先找统一管理器与现有实现，再动手；禁止另起炉灶堆砌补丁**。

## 1. 项目定位与目录

网页图片标注工具（FastAPI 后端 + React 单页前端）。任务根由 `ANNOTATOR_DATA_DIR`
环境变量指定（配置于 `web_annotator/.env`），任务须手动加入清单
（`.annotator_tasks.json`），不会因磁盘子目录自动出现。

```
server.py                      应用入口，装配全局管理器与中间件
routes/                        路由层，只做参数校验/编排，不写业务算法
services/                      业务与基础设施
  core/                        跨业务基础设施：jobs / device / scanner / thumbnailer / http / db
  analysis/                    图像分析：wd14 / birefnet / scoring / dedupe / reranker / sam3
  dataset/                     任务运行时：runtime / layout / crop
  state/                       标注状态：annotator / mask / tasks
  online_sources/              在线图源（danbooru / pixiv / bilibili / x / gelbooru / e621）
  tagging/                     标签清洗与统计
  models/                      Pydantic 响应模型，按域拆分
frontend/                      前端源码（React + TypeScript + Tailwind + Radix）
  dist/                        构建产物（npm run build 生成，不纳入版本库；含页面壳 index.html）
  src/app/                     全站 Shell 与数据集上下文
  src/lib/                     API 客户端、类型、滚动容器
  src/components/              顶栏与跨页组件
  src/components/ui/           全部页面共用的控件与状态构件
  src/filter/                  筛选页与瀑布流
  src/pages/<page>/            各业务页
assets/blocked.html            UA 拦截提示页（静态页，不经前端构建）
assets/configs/                训练配置 Jinja2 模板（子目录名即配置导出格式，就绪页导出用）
docs/                          功能说明文档
```

## 2. 统一管理器（后端）

### 2.1 模型生命周期 —— `services/core/device.py`

所有 GPU/CPU 推理模型统一由 `device` 模块管理，禁止在业务模块内自行 `import torch`
加载/卸载。生命周期为 **初始化到内存 → 加载到 GPU ↔ 卸载到内存 → 超时完全释放**。

- 模块内用 `register_model(load, unload)` 登记模型并持有返回的 `Model` 单例（如
  `_AES_MODEL`）。
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

### 2.5 网络会话 —— `services/core/http.py`

全部出站 HTTP 请求经 `http_session()` 取当前事件循环的会话，禁止自行构造
`httpx.AsyncClient`。会话按协议版本持有复用客户端：连接保活、HTTP/2 多路复用、
并发连接数由池上限约束，请求数量增长不会线性占用文件描述符。

- 来源差异（Referer / Cookie / Accept / 超时 / 是否 HTTP/2）一律作为请求参数传入，
  不为单个来源派生新客户端；图床特性以 `SourceClient.image`（`ImagePolicy`）声明。
- 会话归属其事件循环：主循环在 `server.py:lifespan` 关闭；工作线程内的临时循环
  以 `asyncio.run(run_isolated(coro))` 运行，协程结束即释放连接池。
- 流式响应用 `HttpSession.stream`，迭代器结束时连接归还池；调用方不得关闭会话。

### 2.6 在线源缩略图缓存 —— `services/online_sources/framework/thumb_cache.py`

代理到的缩略图（`kind=thumb`）的字节写入任务根 `.cache/thumb_files/`（按摘要前两位
分片），索引存入任务根 `.cache/thumb_index.db`；`sample` 等大图不缓存。索引经
SQLAlchemy 异步 ORM（`sqlite+aiosqlite`）维护，表含 `key` / `ctype` / `size` 与
自增 `id`。图片字节不得存入数据库，也不得为条目另开旁路元数据文件。

- 淘汰由内存 W-TinyLFU 决定：窗口段（1%）+ 试用段 + 保护段（主区 80%），
  窗口溢出时与试用段受害者按 Count-Min Sketch 频率比较准入；命中提升到保护段。
  禁止以文件时间戳或目录扫描作为淘汰依据。
- 启动时核对索引与文件：索引有行而文件缺失的删行，文件存在而索引无行的删文件，
  运行期两者始终一致。
- 容量由 `ONLINE_THUMB_CACHE_ITEMS`（条目数）与 `ONLINE_THUMB_CACHE_MB`（MB）
  约束，超限优先淘汰低频条目；条目数为 0 时禁用缓存。
- 前端 `thumbUrl` 对所有远程来源统一传 `kind=thumb`，不得只在部分来源启用缓存。
- 响应头 `X-Thumb-Cache` 标记 `hit` / `miss` / `off`，用于核对缓存是否生效。
- 图片响应必须带 `Content-Encoding: identity`（由 `server.py:ImageIdentityMiddleware`
  统一标注），禁止让 GZip 中间件对已压缩的图片二次压缩。
- 数据库连接统一启用 WAL 与 `synchronous=NORMAL`：读写并发互不阻塞，单条提交
  不必每次等待磁盘；缓存写入本身由单写锁串行，读路径不得持有写锁。

### 2.7 数据库层 —— `services/core/db.py`

需要持久化结构化数据时统一用 `Database.open(path)` 取异步引擎与会话工厂，驱动
为 `aiosqlite`；不得在业务模块内自行 `create_async_engine` 或拼接 SQLite 连接。

- 每个业务域声明自己的 `DeclarativeBase` 子类与模型，建表调用
  `await db.create_all(that_base)`，各域的库互不混淆。
- 引擎由装配方（`server.py:lifespan`）创建并在关闭时 `await db.close()`。
- 会话按操作从 `db.sessions()` 取得，写操作由业务自身串行化，禁止跨请求共享会话。

## 3. 前端统一架构

所有业务页是一个 React 单页应用，源码在 `frontend/src/`，构建产物在 `frontend/dist/`
（不纳入版本库）。页面壳即 `frontend/dist/index.html`，由 `frontend/index.html` 经
Vite 生成，其中的脚本与样式引用已按 `base` 重写；服务端不再维护手写页面模板，
只把该壳返回给全部路由，标题由前端按路由设置。服务端把 `frontend/dist/` 挂载到
`/static/app`，与 Vite 的 `base` 一一对应。

- `frontend/src/main.tsx`：按 URL 路径选择页面组件并设置标题；首页 `/` 独立渲染，
  其余页统一包在 `AppShell` 中。
- `frontend/src/app/AppShell.tsx`：全站 Shell。持有数据集级状态（目录多选、设置、提示），
  通过 `AppContext` 暴露 `useApp()`。Shell 是唯一的滚动与布局骨架：根为
  `flex h-dvh flex-col`，顶栏 `shrink-0`，内容区为带 `data-scroll` 的
  `flex min-h-0 flex-1 flex-col overflow-y-auto` 容器。页面不得自行计算视口高度，
  也不得声明 `100vh`、`calc(100vh-...)` 或固定顶栏高度。
- `frontend/src/components/Nav.tsx`：全站唯一的顶栏实现（`NavBar` / `NavLink` /
  `DirectoryPicker` / `Nav`）。首页与业务页都经此渲染，页面不得再手写顶栏 DOM。
- `frontend/src/components/ui/toast.tsx`：`Toast` 与 `useNotifier`，Shell 与首页共用；
  页面不得自建提示容器。
- `frontend/src/lib/scroll.ts`：页面滚动的唯一入口。`scrollContainer()` 取 Shell 的内容
  容器，`captureCardAnchor` / `restoreCardAnchor` 保持列数变化时的阅读位置，
  `useScrollLock` 在模态期间锁定内容容器。页面不得操作 `window.scrollTo`、`window.scrollBy`
  或 `document.body.style.overflow`。
- `frontend/src/lib/api.ts`：唯一的请求层。`apiPath(suffix)` 构造数据集作用域路径，
  `request` 统一抛错，`watchJob` 订阅长任务 SSE，`findActiveJob` 恢复未结束任务。
- `frontend/src/components/ui/`：全部页面共用同一套构件，页面只组合，不重复实现。
  - 基础控件：`Button` / `Input` / `Select` / `Checkbox` / `Slider` / `Dialog`。
  - 工具栏：`Toolbar` / `ToolbarGroup` / `ToolbarDivider`，只承载操作与参数入口。
  - 状态栏：`StatusBar`（`components/ui/status.tsx`），全站唯一，固定在窗口底部，由 Shell
    渲染；页面与对话框通过 `useStatus(segs, summary)` 与 `useJob(state)` 上报数据。
  - 任务反馈：并入状态栏左端（状态、进度、计数、取消），不再有独立的进度条组件。
- `frontend/src/filter/Masonry.tsx`：瀑布流纯组件。按已知宽高比在插入前计算列高，
  图片加载不改变已渲染卡片位置；点击、选择、预览由页面通过回调提供。
- `frontend/src/pages/<page>/`：各业务页。工具栏一律使用 `Toolbar`，不得手写吸附容器。
- 设置键与 `.annotator_settings.json` 的既有键一致（`filter` / `sort` / `booru_ui` /
  `caption` / `scoring` / `dedupe` / `tag_check` / `crop` / `paint` / `ready` 等），
  写入统一走 `useApp().saveSettings(patch, datasetLevel)`。
- 长任务统一用 `api.watchJob`，终态刷新页面数据；不得各页自建 SSE 解析。同一页面并发
  多个任务时，每个任务持有独立的关闭函数与独立的结果槽位，禁止共用一个订阅或快照槽位
  互相顶替。
- 卡片级状态只保留一份真相：远端图片的保存状态由 `downloads` 单表记录（无条目即未保存），
  不得再维护并行的布尔集合；批量与元数据任务各自持有快照，`useJob` 仅在所属对话框打开时
  上报。
- 构建：`cd frontend && npm run build`，产物写入 `frontend/dist/`，禁止手工修改产物，
  也不得把产物提交进版本库。

### 3.1 标签页操作范式

列表类页面（筛选 / 评分 / 去重 / 标签检查 / 描述）遵循同一套交互语义：

1. **工具栏分区从左到右**：本页主操作（`primary`）→ 取消（`danger`，仅任务运行时
   出现）→ `ToolbarDivider` → 参数与筛选 → `ToolbarDivider` → 选择与批量操作。
   工具栏只放操作，主操作只有一个，其余按钮一律中性，破坏性操作使用 `danger`。
2. **主操作职责唯一**：主操作直接提交任务或打开其参数面板，不在两个位置重复放置
   "开始"按钮；参数面板底部只保留保存/关闭。
3. **参数与条件分工**：高频单项输入（标签、搜索词、排序、列数）留在工具栏；低频成组
   参数（阈值、算法、筛选表达式等）一律收进 `Dialog`，由"搜索条件 / 扫描条件 /
   统计条件"入口打开，禁止在工具栏与对话框之间形成两套入口。
4. **选择语义统一**：单击卡片切换选中，Shift 连选，`Ctrl+A` 全选，`Delete` 删除选中项；
   工具栏提供全选 / 反选 / 清除选择，未选中时批量按钮置灰。
5. **预览语义统一**：右键（桌面）或长按（触屏）打开大图或详情，且不改变选中状态；
   Esc 或关闭按钮退出。单击立即切换选中，禁止用单击直接打开模态，也禁止用双击并延迟单击。
6. **任务反馈统一**：任务的状态、进度、计数与取消入口统一出现在全站状态栏左端，由
   `useJob` 上报；页面与对话框不得自绘进度条，也不得在对话框内再放状态条。
7. **状态显示统一**：集合统计、资源状态与摘要一律经 `useStatus` 上报到全站状态栏，
   页面不得渲染自己的状态条，也不得把状态塞进工具栏。
8. **任务目标所见即所得**：列表页的任务作用集合恒为当前瀑布流中的图片——未选中卡片时
   取当前筛选结果，选中卡片时取选中且可见的图片；工具栏统计、批量操作与任务提交共用
   这一集合，禁止另设「范围」下拉或第二套集合计算。
9. **状态语义固定**：状态栏分段只允许五类，按此顺序渲染——
   `total`（集合规模，标签固定"共"）、`nav`（页面导航入口，如当前位置 / 缩略条开关，
   必须给出名称与点击行为）、`part`（分类计数，必须给出分类名）、
   `selected`（选中数，标签固定"已选"）、`resource`（模型/引擎/缓冲等外部资源状态，
   必须给出资源名），摘要文本位于整条最右。任务状态与任务计数位于整条最左；卡片级状态
   （下载中、写回高亮）属于卡片自身，不进入状态栏。`total` 与 `selected` 不得由页面
   另起标签，`part`、`resource` 与 `nav` 不得省略名称；同一含义在不同页面必须同名。

## 4. 标准控件（模板与样式）

控件均定义于 `frontend/src/components/ui/`（Tailwind + Radix），页面只组合组件，不得内联重写。

- **工具栏**：`Toolbar` 统一定位、背景与分区间距，吸附在 Shell 内容容器顶部；分区用
  `ToolbarGroup`，分组之间用 `ToolbarDivider`。页面不得自行声明 `sticky`、`z-30` 或背景。
- **状态栏**：全站唯一，固定在窗口底部（Shell 的最后一个 flex 子项），横跨整个应用，内容
  滚动不影响其位置。左端为任务，右侧为 `total → nav → part → selected → resource`
  五类分段与摘要。页面调用 `useStatus`、对话框与任务提交方调用 `useJob`，不得直接渲染
  状态条。
- **任务反馈**：状态栏左端统一显示状态、进度、计数与取消/清除；终态提示另走
  `useApp().notify`。
- **模态对话框**：`Dialog`（Radix）固定标题、正文与 footer 三段；footer 只放关闭/保存/应用。
- **表单控件**：`Button` / `Input` / `Select` / `Checkbox` / `Slider` 是外观唯一定义处；
  页面只通过 `className` 调整宽度与布局，不重写配色、边框与内边距。
- **筛选输入**：筛选与搜索类文本框使用 `type="search"`，清除入口由浏览器原生清除按钮提供，
  页面不得另放"清空"按钮。
- **大图查看**：页面自建遮罩时使用相对定位的整块覆盖层（`absolute inset-0`），由 Shell 的
  内容容器裁剪；信息区必须阻止点击穿透，Esc 与关闭按钮可退出。
- **提示**：`useApp().notify(msg)`；页面不得自建 toast 容器。

## 5. 代码风格

### 5.1 通用

- 使用中文进行注释与文档；术语专业统一，禁止黑话与不准确简写（如用"持久化/写入"，
  不用"落盘"）。
- **严禁 `try` 吞异常**：不做空 `except` 掩盖错误，也不做无意义的兜底。异常应在能修复的
  上游正确处理，而不是在下游用复杂逻辑弥补。
- **禁止兜底逻辑**：数据应在上游被完善处理。定位问题时先整理既有代码找根因，禁止
  "加一个保底"式的补丁。
- 相同功能必须整合复用，禁止随处复制相似实现；新增功能优先到 `services/` 与
  `frontend/src/` 中查找既有实现。
- 不做与旧代码的兼容补丁，除非用户明确要求。

### 5.2 Python

- 文件顶部 `from __future__ import annotations`；类型注解完整。
- **函数必须有规范 docstring**（Google 风格），严格标识：用途 / Args（含类型）/ Returns
  （含类型）/ Callers。Callers 记录全部引用位置：代码调用（`file:function`）与文档/前端
  引用（`docs/*.md`、`frontend/src/**/*.ts(x)`、`assets/*.html`）。单行简短函数可单行 docstring。
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
