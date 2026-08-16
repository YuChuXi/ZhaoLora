"""蒙板逻辑表达式: 解析与掩码求值.

支持把多个蒙板来源通过 ``+`` (并集) / ``-`` (差集) / 括号组合成最终蒙板.
操作数有两类:

- ``默认蒙板``: 当前已保存的蒙板; 不存在时视为全不透明 (255).
- ``"概念文本"``: SAM3 文本提示概念分割得到的二值掩码, 引号可为
  ASCII ``"`` 或中文全角 ``""``.

表达式示例: ``(默认蒙板-("白色背景"+"对话气泡"))`` 表示从默认蒙板中
减去白色背景与对话气泡的并集区域.

``+`` 与 ``-`` 同级且左结合, 括号提升优先级.

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
    """默认蒙板操作数. 引用: ``parse`` / ``evaluate``. """


@dataclass(frozen=True)
class BinOp:
    """二元运算 ``+`` / ``-``. 引用: ``parse`` / ``evaluate``. """

    op: str
    left: "Expr"
    right: "Expr"


Expr = Union[Concept, Default, BinOp]

# 表达式内可接受的概念引号定界符: ASCII 与中文全角
_QUOTES = ('"', "\u201c", "\u201d")
# 默认蒙板关键字
_DEFAULT_WORD = "默认蒙板"


class ExpressionError(ValueError):
    """表达式语法或语义错误. 引用: ``parse``. """


def _tokenize(expr: str) -> list[str]:
    """将表达式切分为 token 列表.

    空白被跳过; ``(`` ``)`` ``+`` ``-`` 各为独立 token; 引号内的概念文本
    作为一个整体 token (保留原文本); 其余连续字符视为关键字 token.

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
        if ch in "()+-":
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
        j = i
        while j < n and not expr[j].isspace() and expr[j] not in "()+-" and expr[j] not in _QUOTES:
            j += 1
        tokens.append(expr[i:j])
        i = j
    return tokens


class _Parser:
    """递归下降解析器: expr := term (('+'|'-') term)* ; term := '(' expr ')' | 关键字 | 概念. """

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
            if op not in ("+", "-"):
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
        if tok in ("+", "-", ")"):
            raise ExpressionError(f"意外的 token: {tok}")
        self._pos += 1
        if tok == _DEFAULT_WORD:
            return Default()
        return Concept(text=tok)


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
    """掩码并集: 取逐像素较大值 (保持 0-255). 引用: ``evaluate``. """
    return np.maximum(a, b).astype(np.uint8)


def _sub(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """掩码差集: A 减去 B 的区域 (B 非零处 A 置 0). 引用: ``evaluate``. """
    return np.where(b > 0, 0, a).astype(np.uint8)


def evaluate(
    node: Expr,
    concepts: dict[str, np.ndarray | None],
    default: np.ndarray,
) -> np.ndarray:
    """对 AST 求值得到最终掩码.

    概念掩码与默认蒙板须同尺寸; 缺失的概念 (值为 ``None``) 视为全零掩码.
    结果尺寸与 ``default`` 一致, 值域 0-255.

    Args:
        node: ``parse`` 返回的 AST 根节点.
        concepts: 概念名 → 二值掩码 (H, W) 或 ``None``(未命中).
        default: 默认蒙板 (H, W), 作为 ``默认蒙板`` 操作数的值.

    Returns:
        np.ndarray: 最终掩码 (H, W), uint8, 值域 0-255.

    Raises:
        ExpressionError: 掩码尺寸不一致.

    Callers:
        - ``services/analysis/sam3.py:run_sam3_expression_mask``
    """
    if isinstance(node, Default):
        return default
    if isinstance(node, Concept):
        mask = concepts.get(node.text)
        if mask is None:
            return np.zeros(default.shape, dtype=np.uint8)
        return mask
    left = evaluate(node.left, concepts, default)
    right = evaluate(node.right, concepts, default)
    if left.shape != right.shape:
        raise ExpressionError("掩码尺寸不一致")
    return _add(left, right) if node.op == "+" else _sub(left, right)
