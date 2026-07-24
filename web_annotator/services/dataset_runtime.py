"""
单个数据集在运行时的服务句柄。

引用：server.py 启动时构建；routes/deps.py 按请求路径解析。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.dataset_layout import DatasetLayout
from services.state_manager import StateManager
from services.thumbnailer import Thumbnailer


@dataclass
class DatasetRuntime:
    """一个已配置数据集根目录的运行时对象。"""

    name: str
    layout: DatasetLayout
    thumbnailer: Thumbnailer
    state_manager: StateManager
    mask_dir: Path
    color_index: Any = None

    @property
    def root(self) -> Path:
        return self.layout.root

    @property
    def main_dir(self) -> Path:
        return self.layout.main_dir


@dataclass
class DatasetRegistry:
    """进程内已配置数据集表。引用：server.py、routes/deps.py、routes/datasets.py。"""

    by_name: dict[str, DatasetRuntime] = field(default_factory=dict)

    def get(self, name: str) -> DatasetRuntime | None:
        return self.by_name.get(name)

    def names(self) -> list[str]:
        return sorted(self.by_name.keys())

    def add(self, runtime: DatasetRuntime) -> None:
        if runtime.name in self.by_name:
            raise ValueError(f"数据集名称重复: {runtime.name}")
        self.by_name[runtime.name] = runtime
