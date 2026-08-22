#!/usr/bin/env python3
"""校验全项目 Callers 引用是否真实存在.

扫描所有 .py / .js 文件文档字符串中的 ``xxx:func`` 引用,
交叉验证目标文件与目标函数/类是否存在, 输出悬空引用清单.

用法:
    python _check_callers.py
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parent
PY_FILES = list(ROOT.rglob("*.py"))
JS_FILES = list((ROOT / "static" / "js").glob("*.js"))

_CALLER_RE = re.compile(r"Callers:")
# 唯一标准条目格式: "- ``引用``" 后接可选说明文字; 引用仅限双反引号包裹
_ENTRY_RE = re.compile(r"^\s*-\s+``([^`]+)``(?:\s+.*)?$")
# 唯一合法引用格式: file:function / file:Class.method / file:(模块级引用, func 留空但冒号必须保留)
_FILEFUNC_RE = re.compile(r"^[\w./]+\.(?:py|js):[\w.]*$")

total = 0

# JS 函数名提取
_JS_FUNC_RE = re.compile(r"(?:function\s+(\w+)\s*\(|(\w+)\s*:\s*function\s*\(|(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>|\b(\w+)\s*=\s*function)")

# JS 对象方法: obj.method = function 或 methodName() { 在对象字面量内
_JS_OBJ_METHOD_RE = re.compile(r"^\s*(\w+)\s*\(\s*\)\s*\{", re.MULTILINE)


def _py_names(path: Path) -> set[str]:
    """解析 .py 文件中的函数/类/方法名.

    Args:
        path (Path): 目标 .py 文件.

    Returns:
        set[str]: 函数名、类名与 ``类.方法`` 名集合(含 async def 与嵌套函数).
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


def _js_names(path: Path) -> set[str]:
    """提取 .js 文件中的函数名(声明式与对象方法).

    Args:
        path (Path): 目标 .js 文件.

    Returns:
        set[str]: 函数名集合.
    """
    src = path.read_text(encoding="utf-8")
    names = set()
    for m in _JS_FUNC_RE.finditer(src):
        for g in m.groups():
            if g:
                names.add(g)
    names.update(_JS_OBJ_METHOD_RE.findall(src))
    return names


def _locate(target: str, files: list[Path]) -> tuple[Path | None, list[Path]]:
    """按引用路径定位目标文件, 返回(匹配文件, 全部候选).

    Args:
        target: 引用中的文件路径(如 ``routes/tasks.py`` / ``static/js/nav.js``).
        files: 候选文件列表.

    Returns:
        tuple[Path | None, list[Path]]: (最短匹配文件, 全部候选); 无匹配时 (None, []).
    """
    Callers:
        - ``_check_callers.py:_verify``
    # 精确尾部匹配, 取最短(最精确)路径
    cands = [p for p in files if p.as_posix().endswith(target)]
    if not cands:
        norm = target.replace("/", ".")
        cands = [
            p for p in files
            if  p.as_posix().replace("/", ".").endswith(norm + ".py")
            or p.as_posix().replace("/", ".").endswith(norm + ".js")
        ]
    if not cands:
        return None, []
    cands.sort(key=lambda p: len(p.as_posix()))
    return cands[0], cands

def main() -> None:
    """入口: 扫描全部 Callers 条目并报告问题."""
    global total
    dangling: list[tuple[str, int, str, str]] = []
    total = 0

    for path in PY_FILES:
        lines = path.read_text(encoding="utf-8").splitlines()
        in_callers = False
        for i, line in enumerate(lines):
            if _CALLER_RE.search(line):
                in_callers = True
                # 单行格式 "xxx. Callers: yyy." 时本行即含条目
                after = line.split("Callers:", 1)[1]
                m = _ENTRY_RE.match(after)
                if m:
                    total += 1
                    _verify(m.group(1), path, i + 1, dangling)
                continue
            # 非标准块标题(引用: / Caller:) → 报告并要求统一为 Callers:
            if re.match(r"^\s*(?:引用|Caller)[:：]", line):
                dangling.append((path.as_posix(), i + 1, line.strip()[:60], "格式不规范: 块标题应为 Callers:"))
                continue
            if in_callers:
                stripped = line.strip()
                if not stripped:
                    in_callers = False
                    continue
                # 属于 Callers 块的条目(以 - 开头)必须逐条校验
                if stripped.startswith("-"):
                    _check_entry_line(line, path, i + 1, dangling)
                    continue
                # 非条目内容, 退出 Callers 块
                in_callers = False

    print(f"Callers 条目总数: {total}")
    print(f"悬空/错误引用: {len(dangling)}")
    for d in dangling:
        print(f"  {d[0]}:{d[1]}  ->  {d[2]}  ({d[3]})")


def _check_entry_line(line: str, path: Path, lineno: int, dangling: list) -> None:
    """校验 Callers 块中的单个条目行.

    仅接受唯一标准条目格式 ``- ``引用`` [说明]``: 以 ``-`` 开头, 引用必须由
    双反引号包裹并紧跟其后. 不匹配该结构的行(单反引号 / 多引用 / 反引号前有
    其它文字)一律报格式不规范, 要求人工统一, 不得跳过.

    Args:
        line: 条目行原文.
        path: 所在文件.
        lineno: 行号.
        dangling: 悬空/格式违规引用收集列表.
    """
    Callers:
        - ``_check_callers.py:main``
    global total
    stripped = line.strip()
    if not stripped.startswith("-"):
        dangling.append((path.as_posix(), lineno, stripped[:60], "条目行必须以 - 开头"))
        return
    m = _ENTRY_RE.match(line)
    if not m:
        # Callers 块仅允许标准引用条目, 纯说明行(无论是否含反引号)一律报告
        dangling.append((path.as_posix(), lineno, stripped[:80], "格式不规范: 应为 - ``file:function`` [说明]"))
        return
    ref = m.group(1)
    total += 1
    _verify(ref, path, lineno, dangling)


def _verify(ref: str, src: Path, lineno: int, dangling: list) -> None:
    """校验引用是否真实存在, 仅接受唯一合法写法 file:function / file.

    Args:
        ref: 引用原文(如 ``routes/tasks.py:list_tasks`` / ``routes/tasks.py``).
        src: 引用所在的源文件.
        lineno: 引用行号.
        dangling: 悬空引用收集列表.
    """
    Callers:
        - ``_check_callers.py:_check_entry_line``
        - ``_check_callers.py:main``
    if not _FILEFUNC_RE.match(ref):
        dangling.append((src.as_posix(), lineno, ref, "格式不规范: 应为 file:function (如 routes/tasks.py:list_tasks)"))
        return
    target, sep, func = ref.partition(":")

    if target.endswith(".js"):
        tgt, cands = _locate(target, JS_FILES)
        if tgt is None:
            dangling.append((src.as_posix(), lineno, ref, "JS 目标文件不存在"))
            return
        if len(cands) > 1:
            dangling.append((src.as_posix(), lineno, ref, f"路径歧义: {target} 匹配 {len(cands)} 个文件, 需写完整相对路径"))
            return
        if func and func not in JS_INDEX.get(tgt, set()):
            dangling.append((src.as_posix(), lineno, ref, f"{tgt.as_posix()} 中无函数 {func}"))
        return

    tgt, cands = _locate(target, PY_FILES)
    if tgt is None:
        dangling.append((src.as_posix(), lineno, ref, "目标文件不存在"))
        return
    if len(cands) > 1:
        dangling.append((src.as_posix(), lineno, ref, f"路径歧义: {target} 匹配 {len(cands)} 个文件, 需写完整相对路径"))
        return
    if func and func not in PY_INDEX.get(tgt, set()):
        dangling.append((src.as_posix(), lineno, ref, f"{tgt.as_posix()} 中无函数 {func}"))


if __name__ == "__main__":
    PY_INDEX = {p: _py_names(p) for p in PY_FILES}
    JS_INDEX = {p: _js_names(p) for p in JS_FILES}
    main()
