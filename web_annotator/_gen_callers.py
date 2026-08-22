#!/usr/bin/env python3
"""基于 AST 解析的实际调用关系分析器.

扫描全部 .py 文件语法树, 建立「被调符号 → 实际调用者(file:func)」映射,
供 Callers 文档校验与自动修复使用, 避免猜测式修复.

覆盖场景:
- 模块级函数 / 类方法(含 ``self``/``cls`` 调用的本类方法) / 静态方法;
- 别名导入(``import x.y as z`` / ``from x.y import a as b``);
- 直接函数名 / ``模块.函数`` / ``类.方法`` 形式调用.

局限(不静态可判定的间接调用): 回调闭包作为参数传递、实例方法经变量分发、
外部框架调度(装饰器注册/路由)不计入, 需人工补充.

用法:
    python _gen_callers.py             # 打印全部符号的调用者
    python _gen_callers.py 符号名       # 打印指定符号的调用者
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
PY_FILES = list(ROOT.rglob("*.py"))

# 导入绑定: (file, 局部名) -> ("module", 模块路径) 或 ("symbol", 模块路径, 符号名)
_IMPORT_BIND: dict[tuple[Path, str], tuple[str, ...]] = {}

# 符号表: file -> {符号名: (类型, 所属类名或 "")}  类型: "func" | "class"
_SYMBOLS: dict[Path, dict[str, tuple[str, str]]] = {}

# 类方法表: file -> {类名: set(方法名)}
_CLASSES: dict[Path, dict[str, set[str]]] = {}

# 调用关系: (被调 file, 被调符号) -> set(调用者 "file:符号")
_CALLS: dict[tuple[Path, str], set[str]] = defaultdict(set)


def _module_to_file(mod: str) -> Path | None:
    """模块点号路径 → 文件(普通模块或包 __init__.py).

    Args:
        mod (str): 模块路径, 如 ``routes.deps`` / ``services.models``.

    Returns:
        Path | None: 匹配文件; 无则 None.
    """
    p = ROOT / (mod.replace(".", "/") + ".py")
    if p.exists() and p.is_file():
        return p
    p = ROOT / (mod.replace(".", "/") + "/__init__.py")
    if p.exists() and p.is_file():
        return p
    return None


def _scan_imports(path: Path) -> None:
    """解析文件内的 import 语句, 记录局部名绑定.

    Args:
        path (Path): 目标 .py 文件.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                _IMPORT_BIND[(path, local)] = ("module", alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # 相对导入 → 跳过(本文件内符号解析)
                continue
            for alias in node.names:
                local = alias.asname or alias.name
                _IMPORT_BIND[(path, local)] = ("symbol", base, alias.name)


def _scan_symbols(path: Path) -> None:
    """收集文件的顶层函数/类/类方法符号.

    Args:
        path (Path): 目标 .py 文件.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return
    symbols: dict[str, tuple[str, str]] = {}
    classes: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols[node.name] = ("func", "")
        elif isinstance(node, ast.ClassDef):
            symbols[node.name] = ("class", "")
            methods = {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            classes[node.name] = methods
            for m in methods:
                symbols[m] = ("method", node.name)
    _SYMBOLS[path] = symbols
    _CLASSES[path] = classes


def _caller_name(path: Path, func_name: str, cls: str | None) -> str:
    """构造调用者标识 ``file:func`` / ``file:Class.method``.

    Args:
        path (Path): 调用者所在文件.
        func_name (str): 调用者函数名.
        cls (str | None): 所属类名(方法时).

    Returns:
        str: 调用者标识.
    """
    base = path.relative_to(ROOT).as_posix()
    return f"{base}:{cls + '.' if cls else ''}{func_name}"


def _iter_func_defs(tree: ast.AST, cls: str | None = None, outer: str | None = None):
    """递归遍历函数/方法定义, 产出 (定义名, 所属类, 外层函数名).

    嵌套闭包(如任务 ``run``/``emit`` 回调)内部调用归属其最外层函数,
    避免把闭包名误记为类方法调用者.

    Args:
        tree (ast.AST): 语法树节点.
        cls (str | None): 当前所属类名.
        outer (str | None): 当前最外层函数名.

    Yields:
        tuple: (函数名, 所属类名或 None, 外层函数名或 None).
    """
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, cls, outer
            yield from _iter_func_defs(node, cls, node.name)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield sub.name, node.name, None
                    yield from _iter_func_defs(sub, node.name, sub.name)


def _resolve_call(func: ast.expr, path: Path, cls: str | None) -> list[tuple[Path, str, str]]:
    """解析调用目标, 返回 [(目标文件, 目标符号, 方法所属类或 "")].

    Args:
        func (ast.expr): Call.func 节点.
        path (Path): 调用者所在文件.
        cls (str | None): 调用者所属类名(方法内调用时).

    Returns:
        list: 可能命中的目标列表.
    """
    if isinstance(func, ast.Name):
        name = func.id
        bind = _IMPORT_BIND.get((path, name))
        if bind is not None and bind[0] == "symbol":
            f = _module_to_file(bind[1])
            return [(f, bind[2], "")] if f else []
        if bind is not None and bind[0] == "module":
            f = _module_to_file(bind[1])
            return [(f, "", "")] if f else []
        # 同文件顶层符号
        sym = _SYMBOLS.get(path, {}).get(name)
        if sym is not None:
            return [(path, name, sym[1])]
        return []
    if isinstance(func, ast.Attribute):
        obj, attr = func.value, func.attr
        if isinstance(obj, ast.Name):
            vname = obj.id
            bind = _IMPORT_BIND.get((path, vname))
            if bind is not None and bind[0] == "module":
                f = _module_to_file(bind[1])
                return [(f, attr, "")] if f else []
            if bind is not None and bind[0] == "symbol":
                f = _module_to_file(bind[1])
                sym = _SYMBOLS.get(f, {}).get(bind[2]) if f else None
                if sym is not None and sym[0] == "class":
                    return [(f, attr, bind[2])] if f else []
                return [(f, attr, "")] if f else []
            if vname in ("self", "cls") and cls:
                return [(path, attr, cls)]
            # 同文件类名.方法(静态/类方法)
            if cls is None and vname in _CLASSES.get(path, {}):
                return [(path, attr, vname)]
            # 同文件模块名.函数
            if vname == path.stem:
                return [(path, attr, "")]
            return []
        if isinstance(obj, ast.Attribute) and isinstance(obj.value, ast.Name):
            # 模块路径.函数: a.b.func 形式, 取完整点号路径
            parts = [obj.value.id, obj.attr, func.attr]
            f = _module_to_file(".".join(parts))
            return [(f, func.attr, "")] if f else []
    return []


def _collect_funcs(tree: ast.AST) -> list[tuple[ast.AST, str]]:
    """收集模块级函数与类方法(不含嵌套函数/闭包).

    Args:
        tree (ast.AST): 模块语法树.

    Returns:
        list: [(函数节点, 所属类名或 "")]
    """
    out: list[tuple[ast.AST, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node, ""))
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append((sub, node.name))
    return out


def _analyze() -> None:
    """解析全部文件, 建立调用关系 _CALLS."""
    for path in PY_FILES:
        _scan_imports(path)
    for path in PY_FILES:
        _scan_symbols(path)
    for path in PY_FILES:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for fname, cls, outer in _iter_func_defs(tree):
            func_node = None
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fname:
                    func_node = node
                    break
            if func_node is None:
                continue
            for call in ast.walk(func_node):
                if not isinstance(call, ast.Call):
                    continue
                for tgt_file, tgt_sym, tgt_cls in _resolve_call(call.func, path, cls):
                    if tgt_file is None:
                        continue
                    key = (tgt_file, f"{tgt_cls + '.' if tgt_cls else ''}{tgt_sym}")
                    _CALLS[key].add(_caller_name(path, outer or fname, cls))


def _func_callers(path: Path, name: str, cls: str | None) -> list[str]:
    """查询符号的实际调用者(按 file:func 排序).

    Args:
        path (Path): 符号定义文件.
        name (str): 函数名.
        cls (str | None): 所属类名(方法时).

    Returns:
        list[str]: 调用者 ``file:func`` 列表.
    """
    key = (path, f"{cls + '.' if cls else ''}{name}")
    return sorted(_CALLS.get(key, ()))


def _insert_missing_callers() -> int:
    """为缺少 ``Callers:`` 块的 docstring 自动补全.

    仅插入 AST 确认的实际 Python 调用者; 已有 ``Callers:`` 块、没有调用者或
    单行 docstring 的函数不修改. 前端/回调等间接调用需人工补充.
    工具脚本自身(_*.py)不参与补全.

    Returns:
        int: 补全的 Callers 块数量.
    """
    count = 0
    for path in PY_FILES:
        if path.name.startswith("_"):
            continue
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except Exception:
            continue
        inserts: list[tuple[int, list[str]]] = []
        for node, cls in _collect_funcs(tree):
            if not node.body or not isinstance(node.body[0], ast.Expr):
                continue
            doc = node.body[0].value
            if not isinstance(doc, ast.Constant) or not isinstance(doc.value, str):
                continue
            if "Callers:" in doc.value or doc.end_lineno == doc.lineno:
                continue
            callers = _func_callers(path, node.name, cls)
            if not callers:
                continue
            indent = " " * doc.col_offset
            lines = [f"{indent}Callers:"]
            lines += [f"{indent}    - ``{c}``" for c in callers]
            # 插入在 docstring 闭合行之前, 使其成为 docstring 内容
            inserts.append((doc.end_lineno - 1, lines))
        if not inserts:
            continue
        lines = src.splitlines()
        for lineno, ins in sorted(inserts, reverse=True):
            lines[lineno:lineno] = ins
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        count += len(inserts)
    return count


def main() -> None:
    """入口: 默认打印调用关系; --write 时自动补全缺失的 Callers 块."""
    _analyze()
    if "--write" in sys.argv:
        n = _insert_missing_callers()
        print(f"自动补全 Callers 块: {n}")
        return
    target = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "--write" else None
    shown = 0
    for (tgt_file, tgt_sym), callers in sorted(_CALLS.items()):
        if target and tgt_sym != target:
            continue
        shown += 1
        rel = tgt_file.relative_to(ROOT).as_posix()
        print(f"{rel}:{tgt_sym}")
        for c in sorted(callers):
            print(f"    <- {c}")
    if target and shown == 0:
        print(f"未找到符号: {target}")


if __name__ == "__main__":
    main()
