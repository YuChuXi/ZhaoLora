"""设备检测与 GPU 模型生命周期统一管理.

生命周期模型: **初始化到内存 → 转移到 GPU ↔ 卸载回内存 → 超时完全释放**.

- ``cuda_device``: 惰性检测可用 CUDA 设备并缓存; 环境变量 ``ANNOTATOR_CPU_ONLY``
  启用时恒返回 ``None`` (强制 CPU 推理).
- ``release_cuda``: 归还未用显存, 供卸载后复用.
- ``Model``: 封装单个模型的完整生命周期, 以对象而非名字维系.

  生命周期由 ``Model`` 对象自持 (线程安全), 与对象绑定:

  - 初始化到内存: 首次 ``acquire`` 时 ``_load`` 从磁盘载入权重 (恒在 CPU 内存).
  - 任务使用: ``acquire`` 增加引用并确保权重在目标设备 (CUDA 可用时经 ``move``
    转移到 GPU, CPU-only 时留驻内存), 返回句柄供推理使用.
  - 任务结束只释放: ``release`` 引用归零后权重**卸载回 CPU 内存**驻留, 不销毁,
    仅启动空闲计时.
  - 超时完全释放: 空闲超过 ``_IDLE_TIMEOUT`` 仍无引用时, 才真正 ``unload``
    销毁并清空. 这样避免频繁重建模型, 短时间内再次使用直接复用内存中的权重.

- ``register_model``: 登记模型并返回其 ``Model`` 单例, 供模块持有所属模型对象.
- ``Model.auto_release``: 方法装饰器, 进入时 ``acquire`` 并以固定形参 ``handle``
  注入真实句柄, 退出时 ``release``.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

# 设备与模型生命周期管理日志
logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # torch 惰性导入, 仅用于类型注解
    import torch

_TORCH_DEVICE: torch.device | None = None
_DEVICE_CHECKED = False

# 空闲超时(秒): 模型引用归零后, 超过该时长仍无引用则彻底释放
_IDLE_TIMEOUT = 10 * 60

# 设备转移回调签名: (句柄, 目标设备) -> 转移后的句柄
MoveFn = Callable[[object, "torch.device"], object]


def _default_move(handle: object, device: torch.device) -> object:
    """默认设备转移: 把句柄中的模型权重迁移到目标设备.

    句柄为 ``nn.Module`` 或其元组; 元组中与设备无关的附属对象
    (processor / 预处理变换等) 原样保留.

    Args:
        handle: ``load`` 回调返回的模型句柄, ``nn.Module`` 或含 ``nn.Module``
            的元组.
        device: 转移目标设备.

    Returns:
        object: 完成转移后的句柄, 结构与入参一致.

    Callers:
        - ``services/core/device.py:Model.acquire``
        - ``services/core/device.py:Model.release``
    """
    import torch.nn as nn

    if isinstance(handle, tuple):
        return tuple(x.to(device) if isinstance(x, nn.Module) else x for x in handle)
    return handle.to(device)


def cuda_device() -> torch.device | None:
    """返回可用的 CUDA 设备对象, 无 CUDA 或未安装 torch 时返回 ``None``.

    惰性检测并缓存, 供 FFT 特征提取与两两相似度对比等计算及模型转移复用.
    环境变量 ``ANNOTATOR_CPU_ONLY`` 为 ``1`` / ``true`` / ``yes`` 时强制 CPU,
    返回 ``None``; 调用方据返回值决定是否使用 GPU.

    Returns:
        torch.device | None: CUDA 设备对象, 不可用时为 ``None``.

    Callers:
        - ``services/core/device.py:Model.acquire``
        - ``services/core/device.py:Model.release``
        - ``services/analysis/dedupe.py:extract_fft_features``
        - ``services/analysis/dedupe.py:find_duplicate_groups``
    """
    global _TORCH_DEVICE, _DEVICE_CHECKED
    if not _DEVICE_CHECKED:
        _DEVICE_CHECKED = True
        if os.environ.get("ANNOTATOR_CPU_ONLY", "").lower() in ("1", "true", "yes"):
            logger.info("ANNOTATOR_CPU_ONLY 已启用, 所有推理模型强制使用 CPU")
        else:
            try:
                import torch
                _TORCH_DEVICE = torch.device("cuda") if torch.cuda.is_available() else None
            except ImportError:
                _TORCH_DEVICE = None
    return _TORCH_DEVICE


def _cpu_device() -> torch.device:
    """返回 CPU 设备对象.

    Returns:
        torch.device: ``torch.device("cpu")``.

    Callers:
        - ``services/core/device.py:Model.release``
    """
    import torch

    return torch.device("cpu")


def release_cuda() -> None:
    """回收可释放的 CUDA 显存缓存.

    模型卸载后调用 ``torch.cuda.empty_cache()`` 归还未用显存, 供其他任务复用.
    无 CUDA 或未安装 torch 时静默跳过.

    Callers:
        - ``services/core/device.py:Model._schedule_eviction`` (内部 ``evict`` 定时器回调)
    """
    if not _DEVICE_CHECKED:
        cuda_device()
    if _TORCH_DEVICE is None:
        return
    try:
        import torch
        torch.cuda.empty_cache()
    except ImportError:  # torch 不可用时不具备卸载场景
        return


class Model:
    """单个 GPU 模型的句柄, 自持加载 / 设备转移 / 引用计数 / 驻留复用 / 超时释放.

    Callers:
        - ``services/core/device.py:register_model``
        - ``services/analysis/reranker.py:``
        - ``services/analysis/sam3.py:``
    """

    def __init__(
        self,
        load: Callable[[], object],
        unload: Callable[[object], None],
        move: MoveFn | None = None,
    ) -> None:
        """初始化模型句柄.

        Args:
            load: 加载回调, 无参, 恒将权重载入 CPU 内存并返回真实模型句柄.
            unload: 卸载回调, 接收真实模型句柄并释放其占用的资源.
            move: 设备转移回调 ``(句柄, 目标设备) -> 句柄``; 缺省时使用
                ``_default_move`` (递归迁移容器中的 ``nn.Module``).

        Callers:
            - ``services/core/device.py:register_model``
        """
        self._load = load
        self._unload = unload
        self._move: MoveFn = move or _default_move
        self._handle: object | None = None
        self._refs = 0
        self._lock = threading.Lock()
        # 从加载回调所属模块派生模型名, 使日志可定位到具体模型.
        self._name = (getattr(load, "__module__", "") or "model").rsplit(".", 1)[-1]

    def auto_release(self, func: Callable) -> Callable:
        """装饰器: 包装任务执行函数, 自动持有并注入本模型句柄, 退出时释放.

        任务进入前 ``acquire`` 增加引用并确保权重在目标设备, 将真实句柄以固定
        形参名 ``handle`` 注入被装饰函数; 退出 (成功或异常) 时经 ``finally``
        调用 ``release``. 引用归零后权重卸载回 CPU 内存驻留, 空闲超时才彻底
        释放; 并行任务共享本模型时由引用计数保证不误卸载.

        Args:
            func: 任务执行函数, 需声明形参 ``handle`` 以接收模型句柄.

        Returns:
            包装后的函数.

        Callers:
            - ``services/analysis/reranker.py:rerank`` (@_MODEL.auto_release)
            - ``services/analysis/sam3.py:run_concept_query`` (@_MODEL.auto_release)
            - ``services/analysis/sam3.py:run_sam3_expression_mask`` (@_MODEL.auto_release)
            - ``services/analysis/birefnet.py:run_birefnet_mask`` (@_MODEL.auto_release)
            - ``services/analysis/wd14.py:run_batch`` (@_MODEL.auto_release)
        """

        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> object:
            handle = self.acquire()
            try:
                return func(*args, handle=handle, **kwargs)
            finally:
                self.release()

        return cast(Callable, wrapper)

    def __enter__(self) -> object:
        """上下文管理入口: 进入 ``with`` 块时 ``acquire`` 并返回真实句柄.

        Returns:
            真实模型句柄, 供 ``with model as handle`` 绑定使用.

        Callers:
            - ``services/analysis/scoring.py:run_scoring``
        """
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        """上下文管理出口: 离开 ``with`` 块时归还引用.

        无论块内成功或异常都会调用 ``release``; 引用归零后权重卸载回内存驻留,
        由超时机制负责彻底释放.

        Args:
            *exc: 未使用, 仅为满足上下文管理器协议签名.

        Callers:
            - ``services/analysis/scoring.py:run_scoring``
        """
        self.release()

    def acquire(self) -> object:
        """获取真实模型句柄并增加引用计数 (线程安全).

        首次调用触发 ``load`` 回调载入权重 (恒在 CPU 内存); 随后经 ``move``
        把权重转移到目标设备 (CUDA 可用时上 GPU, CPU-only 时留驻内存) —
        转移幂等, 对已驻留 GPU 的权重无操作. 使用完毕后须调用 ``release`` 归还.

        Returns:
            真实模型句柄, 供推理使用.

        Callers:
            - ``services/core/device.py:Model.__enter__``
            - ``services/core/device.py:Model.auto_release``
        """
        with self._lock:
            if self._handle is None:
                self._handle = self._load()
                logger.info("模型 %s 加载完成并驻留内存", self._name)
            dev = cuda_device()
            self._handle = self._move(self._handle, dev if dev is not None else _cpu_device())
            self._refs += 1
            return self._handle

    def release(self) -> None:
        """归还引用, 归零后权重卸载回 CPU 内存驻留并安排超时彻底释放 (线程安全).

        引用减一; 归零时**不销毁模型**, 而是把权重从 GPU 卸载回 CPU 内存
        (释放显存供其他任务复用), 仅启动空闲计时 (见 ``_schedule_eviction``);
        仅当空闲超时仍无引用才真正卸载销毁.

        Callers:
            - ``services/core/device.py:Model.__exit__``
            - ``services/core/device.py:Model.auto_release``
        """
        with self._lock:
            self._refs -= 1
            if self._refs > 0:
                return
            self._refs = 0
            if self._handle is not None and cuda_device() is not None:
                self._handle = self._move(self._handle, _cpu_device())
        logger.info("模型 %s 引用归零, 权重卸载回内存驻留", self._name)
        self._schedule_eviction()

    def _schedule_eviction(self) -> None:
        """为已空闲(引用归零)的模型安排超时彻底释放.

        延迟 ``_IDLE_TIMEOUT`` 后检查: 若期间被重新获取(引用 > 0)或已释放则放弃;
        否则调用 ``_unload`` 销毁并清空句柄.

        Callers:
            - ``services/core/device.py:Model.release``
        """

        def evict() -> None:
            with self._lock:
                if self._refs > 0 or self._handle is None:
                    return  # 期间被复用或已由其他路径释放
                handle = self._handle
                self._handle = None
            logger.info("模型 %s 空闲超时, 彻底释放", self._name)
            self._unload(handle)
            release_cuda()

        timer = threading.Timer(_IDLE_TIMEOUT, evict)
        timer.daemon = True
        timer.start()


def register_model(
    load: Callable[[], object],
    unload: Callable[[object], None],
    move: MoveFn | None = None,
) -> Model:
    """登记一个 GPU 模型并返回其 ``Model`` 单例.

    Args:
        load: 加载回调, 无参, 恒将权重载入 CPU 内存并返回真实模型句柄.
        unload: 卸载回调, 接收真实模型句柄并释放其占用的资源.
        move: 设备转移回调 ``(句柄, 目标设备) -> 句柄``; 缺省时使用
            ``_default_move`` (递归迁移容器中的 ``nn.Module``).

    Returns:
        与模型对应的 ``Model`` 对象, 模块应持有该对象供 ``auto_release`` 使用.

    Callers:
        - ``services/analysis/reranker.py:``
        - ``services/analysis/sam3.py:``
        - ``services/analysis/wd14.py:``
        - ``services/analysis/birefnet.py:``
        - ``services/analysis/scoring.py:``
    """
    return Model(load, unload, move)
