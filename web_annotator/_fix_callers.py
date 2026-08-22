#!/usr/bin/env python3
"""安全自动统一全项目 Callers 引用格式.

仅处理「整行 = 单个反引号标识符」的纯引用条目, 绝不修改含说明文字的行:

1. 单反引号统一为双反引号;
2. 类.方法 引用(如 ``PixivSource.search``) → 定位类定义文件, 补全
   ``services/online_sources/pixiv/source.py:PixivSource.search``;
3. 模块路径引用(如 ``routes.dedupe.scan_duplicates``) → 末段为函数名,
   前段转文件路径, 验证存在后补全 ``routes/dedupe.py:scan_duplicates``;
4. 单段函数名(如 ``process_one``) → 定位唯一文件补全;
5. 含说明文字的行 / 无法判定的条目 → 列入待人工清单, 不修改.

用法:
    python _fix_callers.py [--dry-run]
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PY_FILES = list(ROOT.rglob("*.py"))

_CALLER_RE = re.compile(r"Callers:")
# 标准条目: "- ``引用``" + 可选说明(双反引号)
_ENTRY_DQ = re.compile(r"^(\s*-\s+)``([^`]+)``(.*)$")
# 待统一: "- `引用`" + 可选说明(单反引号)
_ENTRY_SQ = re.compile(r"^(\s*-\s+)`([^`]+)`(.*)$")
# 行内斜杠分隔的后续引用(拆为独立条目): " / ``ref``"
_CONT_REF_RE = re.compile(r"^\s*/\s*``([^`]+)``\s*(.*)$")
# 已合规 file:function / file:Class.method / file:(模块级引用, func 留空但冒号必须保留)
_FILEFUNC_RE = re.compile(r"^[\w./]+\.(?:py|js):[\w.]*$")
# 纯标识符(函数名 / 类.方法 / 模块路径)
_IDENT_RE = re.compile(r"^[\w.]+$")


def _py_names(path: Path) -> set[str]:
    """解析 .py 文件的函数/类/方法名.

    Args:
        path (Path): 目标文件.

    Returns:
        set[str]: 函数名、类名与 ``类.方法`` 名.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(f"{node.name}.{sub.name}")
    return names


def _rel(path: Path) -> str:
    """相对 ROOT 的 posix 路径.

    Args:
        path (Path): 文件路径.

    Returns:
        str: 相对路径字符串.
    """
    return path.relative_to(ROOT).as_posix()


def _locate_class(cls: str, index: dict[Path, set[str]]) -> Path | None:
    """定位类定义文件.

    Args:
        cls: 类名.
        index: 全部文件符号索引.

    Returns:
        Path | None: 唯一定义类名的文件; 无或歧义返回 None.
    """
    hits = [p for p, names in index.items() if cls in names]
    return hits[0] if len(hits) == 1 else None


def _locate_func(func: str, src: Path, index: dict[Path, set[str]]) -> Path | None:
    """定位函数唯一定义文件(优先当前文件).

    Args:
        func: 函数名或 ``类.方法``.
        src: 引用所在文件.
        index: 全部文件符号索引.

    Returns:
        Path | None: 唯一匹配文件; 无或歧义返回 None.
    """
    if func in index.get(src, set()):
        return src
    last = func.split(".")[-1]
    hits = [p for p, names in index.items() if func in names or last in names]
    return hits[0] if len(hits) == 1 else None


def _locate_file(mod: str, index: dict[Path, set[str]], src: Path | None = None) -> Path | None:
    """按模块名定位文件(完整点号路径或裸模块名).

    Args:
        mod: 模块名(如 ``routes.tag_stats`` / ``dedupe.py`` / ``mask_expression``).
        index: 全部文件符号索引.
        src: 引用所在文件, 用于同目录优先消歧.

    Returns:
        Path | None: 匹配文件; 无或歧义返回 None.
    """
    # 完整点号路径 → 模块文件
    p = ROOT / (mod.replace(".", "/") + ".py")
    if p.exists() and p.is_file():
        return p
    # 完整点号路径 → 包 __init__.py
    p = ROOT / (mod.replace(".", "/") + "/__init__.py")
    if p.exists() and p.is_file():
        return p
    # 直接文件路径形式
    p1 = ROOT / mod
    if p1.exists() and p1.is_file():
        return p1
    # 部分相对路径(如 analysis/dedupe.py) → 全项目 endswith 唯一匹配
    if "/" in mod:
        hits = [p for p in index if p.as_posix().endswith(mod)]
        return hits[0] if len(hits) == 1 else None
    # 裸文件名(如 dedupe.py) → 优先同目录
    if mod.endswith(".py"):
        if src is not None:
            same_dir = src.parent / mod
            if same_dir.exists() and same_dir.is_file():
                return same_dir
        hits = [p for p in index if p.name == mod]
        return hits[0] if len(hits) == 1 else None
    # 裸模块名(如 mask_expression) → 唯一文件名匹配
    fname = mod.split(".")[-1] + ".py"
    if src is not None:
        same_dir = src.parent / fname
        if same_dir.exists() and same_dir.is_file():
            return same_dir
    hits = [p for p in index if p.name == fname]
    return hits[0] if len(hits) == 1 else None


def _fix_ref(ref: str, src: Path, index: dict[Path, set[str]]) -> str | None:
    """将标识符补全为 file:function.

    支持形式: ``Class.method`` / ``module.sub.func`` / ``module:func`` /
    ``module:Class.method`` / 单段函数名.

    Args:
        ref: 引用标识符.
        src: 引用所在文件.
        index: 全部文件符号索引.

    Returns:
        str | None: 补全后的 file:function; 无法判定返回 None.
    """
    if _FILEFUNC_RE.match(ref):
        # 已合规 file:function / file:, 但需检查短文件名歧义 → 补全完整相对路径
        fpath, _, func = ref.partition(":")
        tgt = _locate_file(fpath, index, src)
        if tgt is None:
            return None
        if not func or func in index.get(tgt, set()):
            return f"{_rel(tgt)}:{func}"
        return None
    if ":" in ref:
        mod, rest = ref.split(":", 1)
        if not re.match(r"^[\w.]*$", rest):
            return None
        cand = _locate_file(mod, index, src) if mod else None
        if cand is None:
            if "." in rest and rest.split(".")[0][:1].isupper():
                cand = _locate_class(rest.split(".")[0], index)
            else:
                cand = _locate_func(rest.split(".")[-1], src, index)
        if cand is not None and (not rest or rest in index.get(cand, set())):
            return f"{_rel(cand)}:{rest}"
        return None
    if not _IDENT_RE.match(ref):
        # 文件路径引用(含 / 或 .py/.js 后缀, 如 static/js/booru_filter.js) → 补全完整路径 + ":"
        tgt = _locate_file(ref, index, src)
        return f"{_rel(tgt)}:" if tgt is not None else None
    parts = ref.split(".")
    if len(parts) == 2 and parts[0][:1].isupper():
        # 类.方法 → 定位类文件
        tgt = _locate_class(parts[0], index)
        return f"{_rel(tgt)}:{ref}" if tgt else None
    if len(parts) >= 2 and not parts[0][:1].isupper():
        # 模块路径 → 末段为函数时补全 file:func; 否则整体作为模块路径定位
        mod = ".".join(parts[:-1])
        func = parts[-1]
        cand = _locate_file(mod, index, src)
        if cand is not None and func in index.get(cand, set()):
            return f"{_rel(cand)}:{func}"
        # 整体可能是更长的模块路径(如 services.analysis.reranker)
        tgt = _locate_file(ref, index, src)
        if tgt is not None:
            return f"{_rel(tgt)}:"
        if cand is not None:
            return f"{_rel(cand)}:"  # 纯模块引用 → 前段文件路径 + 空 func
        tgt2 = _locate_func(func, src, index)
        return f"{_rel(tgt2)}:{func}" if tgt2 else None
    if len(parts) == 1 and not parts[0][:1].isupper():
        # 单段小写 → 优先函数名, 其次包名/模块名(如 danbooru)
        tgt = _locate_func(ref, src, index)
        if tgt is not None:
            return f"{_rel(tgt)}:{ref}"
        pkg = ROOT / (parts[0] + "/__init__.py")
        hits = [p for p in index if p.parent.name == parts[0] and p.name == "__init__.py"]
        if pkg.exists():
            return f"{_rel(pkg)}:"
        if len(hits) == 1:
            return f"{_rel(hits[0])}:"
        cand = _locate_file(parts[0], index, src)
        return f"{_rel(cand)}:" if cand else None
    # 单段函数名 → 唯一文件
    tgt = _locate_func(ref, src, index)
    return f"{_rel(tgt)}:{ref}" if tgt else None


def _fix_entry_line(line: str, src: Path, index: dict[Path, set[str]]) -> tuple[list[str], list[str]]:
    """统一单个条目行为唯一标准格式 ``- ``file:function`` [说明]``.

    提取引用(单/双反引号均可)并补全为 file:function; 行内用 ``/`` 分隔的
    多个引用拆分为独立条目行. 无法解析的行原样返回.

    Args:
        line: 条目行原文.
        src: 引用所在文件.
        index: 全部文件符号索引.

    Returns:
        tuple[list[str], list[str]]: (重建后的行列表, 行内引用清单).
    """
    indent = line[: len(line) - len(line.lstrip())]
    m = _ENTRY_DQ.match(line) or _ENTRY_SQ.match(line)
    if not m:
        return [line], []
    ref, rest = m.group(2), m.group(3)
    # 拆分 rest 开头的 " / ``ref``" 连续引用
    refs: list[tuple[str, str]] = [(ref, "")]
    pending = rest
    while True:
        cm = _CONT_REF_RE.match(pending)
        if not cm:
            refs[-1] = (refs[-1][0], pending)
            break
        nref, nrest = cm.groups()
        refs.append((nref, ""))
        pending = nrest
    out: list[str] = []
    for r, tail in refs:
        new_ref = _fix_ref(r, src, index) or r
        t = tail.lstrip()
        out.append(f"{indent}- ``{new_ref}``{(' ' + t) if t else ''}")
    return out, [r for r, _ in refs]


def main() -> None:
    """入口: 扫描并自动修复(默认 dry-run)."""
    dry = "--dry-run" in sys.argv
    index = {p: _py_names(p) for p in PY_FILES}
    fixed: list[tuple[str, int, str, str]] = []

    for path in PY_FILES:
        lines = path.read_text(encoding="utf-8").splitlines()
        new_lines: list[str] = []
        in_callers = False
        for i, line in enumerate(lines):
            if _CALLER_RE.search(line):
                in_callers = True
                new_lines.append(line)
                continue
            if in_callers:
                stripped = line.strip()
                if not stripped:
                    in_callers = False
                    new_lines.append(line)
                    continue
                if stripped.startswith("-"):
                    rebuilt_lines, refs = _fix_entry_line(line, path, index)
                    if rebuilt_lines != [line]:
                        fixed.append((_rel(path), i + 1, line.strip()[:60], " | ".join(refs)))
                        new_lines.extend(rebuilt_lines)
                    else:
                        new_lines.append(line)
                    continue
                # 非条目内容, 退出 Callers 块
                in_callers = False
                new_lines.append(line)
                continue
            new_lines.append(line)

        new_text = "\n".join(new_lines)
        if not dry and new_text != "\n".join(lines):
            path.write_text(new_text + "\n", encoding="utf-8")

    print(f"将补全条目: {len(fixed)}")
    for f in fixed[:40]:
        print(f"  {f[0]}:{f[1]}  {f[2]} -> {f[3]}")
    if len(fixed) > 40:
        print(f"  ... 其余 {len(fixed) - 40} 条")


if __name__ == "__main__":
    main()
