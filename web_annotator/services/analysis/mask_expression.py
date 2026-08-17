"""蒙板逻辑表达式: 解析与掩码求值.

蒙板以 0-255 灰度值表示 (非二值). 表达式运算全程在 0-1 域进行: 数值字面量
直接按 0-1 书写 (1 对应蒙板值 255, 支持任意实数), ``当前蒙板`` 与概念掩码
在求值前除以 255 归一化; 运算结果统一在最后钳位到 0-255 并转换为整数
(``to_uint8``), 中间过程不出现 255 这样的整数域值. 前端输入框外标注 ``×255`` 提示.

支持的运算符 (均为逐像素二元运算, 在 0-1 域计算, 每次结果钳位到 [0, 1]):

- ``+``: 钳位加, ``clip(a+b, 0, 1)``.
- ``-``: 钳位减, ``clip(a-b, 0, 1)``.
- ``*``: 0-1 域乘法, ``clip(a*b, 0, 1)``.
- ``min(a, b)``: 逐像素取较小值.
- ``max(a, b)``: 逐像素取较大值.
- 比较 ``>`` ``<`` ``>=`` ``<=`` ``==`` ``!=``: 逐像素比较, 结果为 0/1 掩码.

操作数有四类:

- ``当前蒙板``: 当前已保存的蒙板; 不存在时视为全不透明 (255).
- ``"概念文本"``: SAM3 文本提示概念分割得到的二值掩码, 引号可为 ASCII ``"``
  或中文全角 ``""``.
- 数值字面量 (0-1 域, 任意实数): 纯值蒙板, 如 ``0`` 全透明 / ``1`` 全不透明 /
  ``0.5`` 半透明 / ``2`` 或 ``-1`` 等中间量亦可.
- 括号与函数式 ``min(a, b)`` / ``max(a, b)``.

表达式示例: ``(当前蒙板-("白色背景"+"对话气泡"))`` 表示从当前蒙板中减去
白色背景与对话气泡相加的区域; ``(当前蒙板*0.5)`` 表示当前蒙板透明度减半.

``+``/``-``/``*`` 同级且左结合, 括号与函数式提升优先级.

Callers:
    - ``services/analysis/sam3.py:run_sam3_expression_mask``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

import numpy as np


@dataclass(frozen=True)
class Concept:
    """SAM3 概念操作数. 引用: ``parse`` / ``evaluate``. """

    text: str


@dataclass(frozen=True)
class Default:
    """当前蒙板操作数. 引用: ``parse`` / ``evaluate``. """


@dataclass(frozen=True)
class Number:
    """数值操作数 (0-1, 对应蒙板值 0-255). 引用: ``parse`` / ``evaluate``. """

    value: float


@dataclass(frozen=True)
class BinOp:
    """二元运算 ``+`` / ``-`` / ``*`` / ``min`` / ``max`` / 比较符. 引用: ``parse`` / ``evaluate``. """

    op: str
    left: "Expr"
    right: "Expr"


Expr = Union[Concept, Default, Number, BinOp]

# 表达式内可接受的概念引号定界符: ASCII 与中文全角
_QUOTES = ('"', "\u201c", "\u201d")
# 当前蒙板关键字
_DEFAULT_WORD = "当前蒙板"
# 独立 token 字符: 括号、二元运算符与函数参数分隔符
_TOKEN_CHARS = set("()+-*,")
# 函数式运算符 (接收两个以逗号分隔的参数)
_FUNC_OPS = ("min", "max")
# 中缀运算符 (左结合)
_INFIX_OPS = ("+", "-", "*", ">", "<", ">=", "<=", "==", "!=")
# 负号字面量的合法前缀: 表达式开头、运算符、左括号、逗号
_PREFIX_CTX = frozenset(_INFIX_OPS + _FUNC_OPS + ("(", ","))


class ExpressionError(ValueError):
    """表达式语法或语义错误. 引用: ``parse``. """


def _tokenize(expr: str) -> list[str]:
    """将表达式切分为 token 列表.

    空白被跳过; ``(`` ``)`` ``+`` ``-`` ``*`` ``,`` 各为独立 token; 比较运算符
    ``>`` ``<`` 为单字符、``>=`` ``<=`` ``==`` ``!=`` 为双字符; 引号内的概念
    文本作为一个整体 token (保留原文本); 以数字或小数点开头的连续片段视为
    数值字面量; 其余连续字符视为关键字 token (``当前蒙板`` / ``min`` / ``max``
    或裸概念).

    Args:
        expr: 原始表达式字符串.

    Returns:
        list[str]: token 列表.

    Raises:
        ExpressionError: 引号未闭合.

    Callers:
        - ``mask_expression:parse``
    """
    tokens: list[str] = []
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "><=!":
            # 比较运算符: 单字符 > <, 双字符 >= <= == !=; 孤立的 = ! 由解析器报错
            if i + 1 < n and expr[i + 1] == "=":
                tokens.append(ch + "=")
                i += 2
            else:
                tokens.append(ch)
                i += 1
            continue
        if ch == "-" and i + 1 < n and (
            expr[i + 1].isdigit()
            or (expr[i + 1] == "." and i + 2 < n and expr[i + 2].isdigit())
        ):
            # 负号字面量 (任意负数): 仅当前一 token 不是操作数结尾 (开头/运算符/括号/逗号)
            # 时并入数字, 如 -1 / -0.5 / 当前蒙板-0.5 中的中缀 - 不受影响
            if not tokens or tokens[-1] in _PREFIX_CTX:
                j = i + 1
                while j < n and (expr[j].isdigit() or expr[j] == "."):
                    j += 1
                tokens.append(expr[i:j])
                i = j
                continue
        if ch in _TOKEN_CHARS:
            tokens.append(ch)
            i += 1
            continue
        if ch in _QUOTES:
            j = i + 1
            buf: list[str] = []
            closed = False
            while j < n:
                if expr[j] in _QUOTES:
                    closed = True
                    break
                buf.append(expr[j])
                j += 1
            if not closed:
                raise ExpressionError(f"引号未闭合: 位置 {i}")
            tokens.append("".join(buf))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and expr[i + 1].isdigit()):
            j = i
            while j < n and (expr[j].isdigit() or expr[j] == "."):
                j += 1
            tokens.append(expr[i:j])
            i = j
            continue
        j = i
        while j < n and not expr[j].isspace() and expr[j] not in _TOKEN_CHARS and expr[j] not in _QUOTES:
            j += 1
        tokens.append(expr[i:j])
        i = j
    return tokens


class _Parser:
    """递归下降解析器: expr := term ((中缀运算符) term)* ;
    term := '(' expr ')' | 'min'/'max' '(' expr ',' expr ')' | 数值 | 关键字 | 概念. """

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._pos = 0

    def parse(self) -> Expr:
        """解析完整表达式并返回 AST 根节点.

        Returns:
            Expr: AST 根节点.

        Raises:
            ExpressionError: token 耗尽或语法错误.

        Callers:
            - ``mask_expression:parse``
        """
        node = self._expr()
        if self._pos != len(self._tokens):
            raise ExpressionError(f"多余内容: {self._tokens[self._pos]}")
        return node

    def _peek(self) -> str | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _expr(self) -> Expr:
        node = self._term()
        while True:
            op = self._peek()
            if op not in _INFIX_OPS:
                return node
            self._pos += 1
            right = self._term()
            node = BinOp(op=op, left=node, right=right)

    def _term(self) -> Expr:
        tok = self._peek()
        if tok is None:
            raise ExpressionError("表达式不完整")
        if tok == "(":
            self._pos += 1
            node = self._expr()
            if self._peek() != ")":
                raise ExpressionError("缺少右括号")
            self._pos += 1
            return node
        if tok in _FUNC_OPS:
            self._pos += 1
            if self._peek() != "(":
                raise ExpressionError(f"{tok} 需要括号参数")
            self._pos += 1
            left = self._expr()
            if self._peek() != ",":
                raise ExpressionError(f"{tok} 需要逗号分隔两个参数")
            self._pos += 1
            right = self._expr()
            if self._peek() != ")":
                raise ExpressionError(f"{tok} 缺少右括号")
            self._pos += 1
            return BinOp(op=tok, left=left, right=right)
        if tok in _INFIX_OPS or tok in (",", ")"):
            raise ExpressionError(f"意外的 token: {tok}")
        self._pos += 1
        if tok == _DEFAULT_WORD:
            return Default()
        try:
            value = float(tok)
        except ValueError:
            return Concept(text=tok)
        return Number(value=value)


def parse(expression: str) -> Expr:
    """解析蒙板逻辑表达式为 AST.

    Args:
        expression: 原始表达式字符串.

    Returns:
        Expr: AST 根节点.

    Raises:
        ExpressionError: 语法错误或引号未闭合.

    Callers:
        - ``services/analysis/sam3.py:run_sam3_expression_mask``
    """
    if not expression or not expression.strip():
        raise ExpressionError("表达式为空")
    return _Parser(_tokenize(expression)).parse()


def extract_concepts(node: Expr) -> list[str]:
    """按出现顺序提取 AST 中全部 SAM3 概念文本 (去重).

    Args:
        node: ``parse`` 返回的 AST 根节点.

    Returns:
        list[str]: 概念文本列表; 无概念时为空列表.

    Callers:
        - ``services/analysis/sam3.py:run_sam3_expression_mask``
    """
    seen: list[str] = []

    def walk(n: Expr) -> None:
        if isinstance(n, Concept):
            if n.text not in seen:
                seen.append(n.text)
        elif isinstance(n, BinOp):
            walk(n.left)
            walk(n.right)

    walk(node)
    return seen


def _add(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """钳位加, 逐像素 clip(a+b, 0, 1). 引用: ``evaluate``. """
    return np.clip(a + b, 0, 1)


def _sub(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """钳位减, 逐像素 clip(a-b, 0, 1). 引用: ``evaluate``. """
    return np.clip(a - b, 0, 1)


def _mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """0-1 域乘法, 逐像素 clip(a*b, 0, 1). 引用: ``evaluate``. """
    return np.clip(a * b, 0, 1)


def _min(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """逐像素取较小值. 引用: ``evaluate``. """
    return np.minimum(a, b)


def _max(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """逐像素取较大值. 引用: ``evaluate``. """
    return np.maximum(a, b)


def _gt(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """逐像素 a > b, 结果 0/1. 引用: ``evaluate``. """
    return (a > b).astype(np.float64)


def _lt(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """逐像素 a < b, 结果 0/1. 引用: ``evaluate``. """
    return (a < b).astype(np.float64)


def _ge(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """逐像素 a >= b, 结果 0/1. 引用: ``evaluate``. """
    return (a >= b).astype(np.float64)


def _le(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """逐像素 a <= b, 结果 0/1. 引用: ``evaluate``. """
    return (a <= b).astype(np.float64)


def _eq(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """逐像素 a == b, 结果 0/1. 引用: ``evaluate``. """
    return (a == b).astype(np.float64)


def _ne(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """逐像素 a != b, 结果 0/1. 引用: ``evaluate``. """
    return (a != b).astype(np.float64)


_OPS = {
    "+": _add, "-": _sub, "*": _mul,
    "min": _min, "max": _max,
    ">": _gt, "<": _lt, ">=": _ge, "<=": _le,
    "==": _eq, "!=": _ne,
}


def evaluate(
    node: Expr,
    concepts: dict[str, np.ndarray | None],
    default: np.ndarray,
) -> np.ndarray:
    """对 AST 求值, 得到 0-1 域浮点掩码 (最终转换见 ``to_uint8``).

    全程在 0-1 域计算: 当前蒙板与概念掩码求值前除以 255 归一化; 数值字面量
    为任意实数直接参与. 缺失的概念 (值为 ``None``) 视为全零掩码. 结果尺寸与
    ``default`` 一致, 值域通常钳位到 [0, 1].

    Args:
        node: ``parse`` 返回的 AST 根节点.
        concepts: 概念名 → 二值掩码 (H, W) 或 ``None``(未命中).
        default: 当前蒙板 (H, W), 0-255 uint8, 作为 ``当前蒙板`` 操作数的值.

    Returns:
        np.ndarray: 中间掩码 (H, W), float64, 值域 [0, 1].

    Raises:
        ExpressionError: 掩码尺寸不一致.

    Callers:
        - ``mask_expression:to_uint8``
    """
    if isinstance(node, Default):
        return default.astype(np.float64) / 255.0
    if isinstance(node, Concept):
        mask = concepts.get(node.text)
        if mask is None:
            return np.zeros(default.shape, dtype=np.float64)
        return mask.astype(np.float64) / 255.0
    if isinstance(node, Number):
        return np.full(default.shape, node.value, dtype=np.float64)
    left = evaluate(node.left, concepts, default)
    right = evaluate(node.right, concepts, default)
    if left.shape != right.shape:
        raise ExpressionError("掩码尺寸不一致")
    return _OPS[node.op](left, right)


def to_uint8(mask: np.ndarray) -> np.ndarray:
    """将 0-1 域掩码统一转换并钳位到 0-255 的 uint8 掩码.

    取整采用四舍五入 (``np.rint``), 越界值 (如数值字面量 2 / -1 的中间结果)
    在此钳位到 [0, 255], 避免直接截断产生偏差.

    Args:
        mask: 0-1 域浮点掩码 (H, W).

    Returns:
        np.ndarray: 0-255 uint8 掩码 (H, W).

    Callers:
        - ``services/analysis/sam3.py:run_sam3_expression_mask``
    """
    return np.clip(np.rint(mask * 255.0), 0, 255).astype(np.uint8)
