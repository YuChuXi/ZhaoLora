"""本地源注册表: 产物按图片 stem 存放于工作子目录内.

本地源与在线图源同处一个命名空间, 但不参与搜索与下载; 它们只在图片被移动、
恢复或删除时随之同步. 各源在自己的模块内调用 ``register_sidecar`` 完成注册.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SidecarSource:
    """一个本地源.

    Attributes:
        name (str): 源名, 与在线图源共享命名空间(如 ``wd14``).
        meta_subdir (str): 该源在工作子目录内的产物目录名(如 ``.wd14_meta``).

    Callers:
        - ``services/analysis/wd14.py:`` : 模块导入时注册.
    """

    name: str
    meta_subdir: str


_SOURCES: dict[str, SidecarSource] = {}


def register_sidecar(source: SidecarSource) -> None:
    """注册本地源.

    Args:
        source (SidecarSource): 待注册的本地源.

    Callers:
        - ``services/analysis/wd14.py:`` : 模块导入时注册.
    """
    _SOURCES[source.name] = source


def sidecar_names() -> list[str]:
    """已注册的本地源名.

    Returns:
        list[str]: 按名称排序的源名.
    """
    return sorted(_SOURCES)


def sidecar_paths(parent: Path, stem: str) -> list[Path]:
    """列出各本地源为指定图片保存的现存产物文件.

    Args:
        parent (Path): 图片所在工作子目录.
        stem (str): 图片文件名 stem.

    Returns:
        list[Path]: 产物文件路径列表(如 ``.wd14_meta/<模型指纹>/<stem>.npy``);
            对应源目录不存在时为空.

    Callers:
        - ``services/state/annotator.py:StateManager._move_meta_files``
        - ``services/online_sources/framework/media.py:remove_download``
    """
    out: list[Path] = []
    for source in _SOURCES.values():
        base = parent / source.meta_subdir
        if not base.is_dir():
            continue
        out.extend(p for p in base.rglob("*") if p.is_file() and p.stem == stem)
    return out
