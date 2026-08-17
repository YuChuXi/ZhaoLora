"""蒙板表达式: 解析与掩码求值.

概述
----
蒙板以 0-255 灰度值表示 (非二值). 表达式运算全程在 0-1 域进行: 数值字面量
直接按 0-1 书写 (1 对应蒙板值 255, 支持任意实数), ``当前蒙板`` 与概念掩码
在求值前除以 255 归一化; 运算结果统一在最后钳位到 0-255 并转换为整数
(``to_uint8``), 中间过程不出现 255 这样的整数域值. 前端输入框外标注 ``×255`` 提示.

语法与优先级
------------
遵循 Python 表达式优先级 (由高到低):

- 括号 ``()`` 与函数式 ``min(a, b)`` / ``max(a, b)`` / ``blur(掩码, 半径)``:
  优先级最高.
- 乘法 ``*``: 0-1 域乘法, ``clip(a*b, 0, 1)``.
- 加减 ``+`` / ``-``: 钳位加 / 钳位减, ``clip(a±b, 0, 1)``.
- 比较 ``>`` ``<`` ``>=`` ``<=`` ``==`` ``!=``: 逐像素比较, 结果为 0/1 掩码;
  不支持链式比较 (如 ``a < b < c``).

``blur(掩码, 半径)`` 对掩码做高斯模糊 (半径单位为像素, 标准差), 用于平滑
SAM 概念分割产生的生硬边缘; 掩码参数可为任意子表达式.

操作数
------
- ``当前蒙板``: 当前已保存的蒙板; 不存在时视为全不透明 (255).
- ``"概念文本"``: SAM3 文本提示概念分割得到的二值掩码, 引号可为 ASCII ``"``
  或中文全角 ``""``.
- 数值字面量 (0-1 域, 任意实数): 纯值蒙板, 如 ``0`` 全透明 / ``1`` 全不透明 /
  ``0.5`` 半透明 / ``2`` 或 ``-1`` 等中间量亦可.

示例
----
``(当前蒙板-("白色背景"+"对话气泡"))`` 表示从当前蒙板中减去白色背景与对话气泡
相加的区域; ``(当前蒙板*0.5)`` 表示当前蒙板透明度减半.

实现说明
--------
解析复用标准库 ``ast`` (Python 表达式语法子集, 自动获得标准运算优先级),
仅接受受支持的节点 (``Constant`` / ``Name`` / ``UnaryOp`` / ``BinOp`` /
``Compare`` / ``Call``), 由自定义求值器 ``evaluate`` 实现逐像素掩码语义,
不执行任意代码.

Callers:
    - ``routes/birefnet.py:mask_generate``
    - ``services/analysis/sam3.py:run_sam3_expression_mask``
"""

from __future__ import annotations

import ast

import cv2
import numpy as np

# 当前蒙板关键字
_CURRENT_WORD = "当前蒙板"
# 函数式运算符 (接收两个以逗号分隔的参数)
_FUNC_OPS = {"min", "max", "blur"}


class ExpressionError(ValueError):
    """表达式语法或语义错误. 引用: ``parse``. """


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


def _blur(mask: np.ndarray, sigma: float) -> np.ndarray:
    """对 0-1 域掩码做高斯模糊, 平滑概念掩码生硬边缘.

    标准差 ``sigma`` 单位为像素; ``sigma <= 0`` 时原样返回。核尺寸取约
    6σ 的奇数并最小为 3, 边界按 OpenCV 反射填充, 避免边缘被腐蚀。

    Args:
        mask: 0-1 域掩码 (H, W)。
        sigma: 高斯核标准差 (像素)。

    Returns:
        np.ndarray: 模糊后的 0-1 域掩码 (H, W)。

    Callers:
        - ``mask_expression:evaluate``
    """
    if sigma <= 0:
        return mask
    ksize = int(round(sigma * 6)) | 1
    if ksize < 3:
        ksize = 3
    return cv2.GaussianBlur(
        mask.astype(np.float32), (ksize, ksize), float(sigma)
    ).astype(np.float64)


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


# 二元算术: ast 运算符类型 → 求值函数
_BIN_OPS = {
    ast.Add: _add,
    ast.Sub: _sub,
    ast.Mult: _mul,
}
# 比较: ast 运算符类型 → 求值函数
_CMP_OPS = {
    ast.Gt: _gt,
    ast.Lt: _lt,
    ast.GtE: _ge,
    ast.LtE: _le,
    ast.Eq: _eq,
    ast.NotEq: _ne,
}


def _normalize(expression: str) -> str:
    """将中文全角引号规范为 ASCII 引号.

    全角引号与 ASCII 引号宽度相同, 替换后字符位置偏移为 0, 不影响语法
    错误定位.

    Args:
        expression: 原始表达式字符串.

    Returns:
        str: 规范化后的表达式.

    Callers:
        - ``mask_expression:parse``
    """
    return expression.replace("\u201c", '"').replace("\u201d", '"')


def parse(expression: str) -> ast.AST:
    """解析蒙板表达式为 AST (基于标准库 ``ast``).

    复用 Python 表达式语法, 自动获得标准运算优先级; 引号支持 ASCII ``"`` 与
    中文全角 ``""``.

    Args:
        expression: 原始表达式字符串.

    Returns:
        ast.AST: Python 表达式 AST (仅含受支持节点).

    Raises:
        ExpressionError: 表达式为空或语法错误.

    Callers:
        - ``routes/birefnet.py:mask_generate``
        - ``services/analysis/sam3.py:run_sam3_expression_mask``
    """
    text = expression.strip()
    if not text:
        raise ExpressionError("表达式为空")
    try:
        return ast.parse(_normalize(text), mode="eval").body
    except SyntaxError as e:
        raise ExpressionError(f"语法错误: {e.msg}")


def extract_concepts(node: ast.AST) -> list[str]:
    """按出现顺序提取 AST 中全部概念文本 (去重).

    Args:
        node: ``parse`` 返回的表达式 AST.

    Returns:
        list[str]: 概念文本列表; 无概念时为空列表.

    Callers:
        - ``services/analysis/sam3.py:run_sam3_expression_mask``
    """
    seen: list[str] = []

    def walk(n: ast.AST) -> None:
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if n.value not in seen:
                seen.append(n.value)
        elif isinstance(n, ast.UnaryOp):
            walk(n.operand)
        elif isinstance(n, ast.BinOp):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, ast.Compare):
            walk(n.left)
            for comparator in n.comparators:
                walk(comparator)
        elif isinstance(n, ast.Call):
            for arg in n.args:
                walk(arg)

    walk(node)
    return seen


def _apply(op, left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """校验尺寸一致后应用逐像素运算. 引用: ``evaluate``. """
    if left.shape != right.shape:
        raise ExpressionError("掩码尺寸不一致")
    return op(left, right)


def _concept_mask(
    concepts: dict[str, np.ndarray | None], text: str, default: np.ndarray
) -> np.ndarray:
    """取概念掩码并归一化到 0-1 域; 缺失的概念视为全零. 引用: ``evaluate``. """
    mask = concepts.get(text)
    if mask is None:
        return np.zeros(default.shape, dtype=np.float64)
    return mask.astype(np.float64) / 255.0


def evaluate(
    node: ast.AST,
    concepts: dict[str, np.ndarray | None],
    default: np.ndarray,
) -> np.ndarray:
    """对 AST 求值, 得到 0-1 域浮点掩码 (最终转换见 ``to_uint8``).

    全程在 0-1 域计算: 当前蒙板与概念掩码求值前除以 255 归一化; 数值字面量
    为任意实数直接参与 (一元负号取负, 越界由 ``to_uint8`` 收敛). 缺失的概念
    (值为 ``None``) 视为全零掩码. 结果尺寸与 ``default`` 一致.

    Args:
        node: ``parse`` 返回的表达式 AST.
        concepts: 概念名 → 二值掩码 (H, W) 或 ``None``(未命中).
        default: 当前蒙板 (H, W), 0-255 uint8, 作为 ``当前蒙板`` 操作数的值.

    Returns:
        np.ndarray: 中间掩码 (H, W), float64, 值域通常钳位到 [0, 1].

    Raises:
        ExpressionError: 不支持的节点/运算符/链式比较/掩码尺寸不一致.

    Callers:
        - ``services/analysis/sam3.py:run_sam3_expression_mask``
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            raise ExpressionError("不支持布尔字面量")
        if isinstance(node.value, (int, float)):
            return np.full(default.shape, float(node.value), dtype=np.float64)
        if isinstance(node.value, str):
            return _concept_mask(concepts, node.value, default)
        raise ExpressionError("不支持的常量类型")
    if isinstance(node, ast.Name):
        if node.id == _CURRENT_WORD:
            return default.astype(np.float64) / 255.0
        return _concept_mask(concepts, node.id, default)
    if isinstance(node, ast.UnaryOp):
        operand = evaluate(node.operand, concepts, default)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return operand
        raise ExpressionError("不支持的一元运算符")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ExpressionError(f"不支持的运算符: {type(node.op).__name__}")
        left = evaluate(node.left, concepts, default)
        right = evaluate(node.right, concepts, default)
        return _apply(op, left, right)
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise ExpressionError("不支持链式比较")
        op = _CMP_OPS.get(type(node.ops[0]))
        if op is None:
            raise ExpressionError("不支持的比较运算符")
        left = evaluate(node.left, concepts, default)
        right = evaluate(node.comparators[0], concepts, default)
        return _apply(op, left, right)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNC_OPS:
            raise ExpressionError("只支持 min / max / blur 函数")
        if len(node.args) != 2:
            raise ExpressionError(f"{node.func.id} 需要两个参数")
        if node.func.id == "blur":
            # 第二参数为数值字面量 (模糊半径), 第一参数可为任意掩码子表达式
            radius = node.args[1]
            if not isinstance(radius, ast.Constant) or not isinstance(
                radius.value, (int, float)
            ):
                raise ExpressionError("blur 的半径必须是数值")
            return _blur(evaluate(node.args[0], concepts, default), float(radius.value))
        left = evaluate(node.args[0], concepts, default)
        right = evaluate(node.args[1], concepts, default)
        op = _min if node.func.id == "min" else _max
        return _apply(op, left, right)
    raise ExpressionError(f"不支持的表达式节点: {type(node).__name__}")


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
