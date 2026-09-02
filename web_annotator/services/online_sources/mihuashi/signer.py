"""米画师签名器: 动态发现站点 signtool WASM 并在 Python 侧复现其调用约定.

米画师所有 ``/api/v1`` 接口要求 ``M-S``(WASM 签名)/``M-T``(Unix 秒)/
``Web-Version`` 三个请求头。签名由站点前端的 wasm-bindgen 产物计算,
WASM 与 glue JS 文件名带 hash、随站方发版变化, 因此不做静态捆绑: 进程
首次签名时从站点 HTML 动态发现 glue chunk 与 WASM 地址并加载。

签名输入 = 纯相对路径(不含 query); 服务端按签名路径记录最大已接受的
``M-T`` 并要求严格递增(相同或更小一律 403 ``signature invalid``), 且
容忍超前时间戳(实测 +600s 可接受), 故按路径维护 ``ts = max(now, last+1)``
的唯一时间戳序列, 无需人为节流。

Callers:
    - ``services/online_sources/mihuashi/client.py:api_get``
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from typing import Any

import wasmtime

from services.online_sources.framework.http import HttpClient

_SITE = "https://www.mihuashi.com"

# 页面 HTML 中的 favicon 与 glue chunk 引用(均为站方模板固定输出)
_ICON_RE = re.compile(r'<link[^>]*rel="[^"]*icon[^"]*"[^>]*href="([^"]+)"')
_HTTP_CHUNK_RE = re.compile(r"https://js-assets\.mihuashi\.com/\S+?/chunk/http\.[\w-]+\.js")
_WASM_RE = re.compile(r"https://js-assets\.mihuashi\.com/\S+?mhs_fe_sign_bg\.[\w-]+\.wasm")

# wasm-bindgen 导入名形如 <基名>_<hex hash>, 去 hash 后按基名分派
_HASH_SUFFIX_RE = re.compile(r"_[0-9a-f]{6,}$")


def _base_name(name: str) -> str:
    """剥离 wasm-bindgen 导入名尾部的 hash 后缀.

    Args:
        name (str): WASM 导入函数名.

    Returns:
        str: 去 hash 的基名.
    """
    return _HASH_SUFFIX_RE.sub("", name)


class JsObj:
    """JS 普通对象(只读属性集合用于模拟 navigator.webdriver 等访问器)."""

    def __init__(self, d: dict | None = None, readonly: tuple = ()) -> None:
        """初始化属性字典与只读属性名集合.

        Args:
            d (dict | None): 初始属性.
            readonly (tuple): 只读属性名, Reflect.set 对其直接返回 false.
        """
        self.d = d or {}
        self.readonly = frozenset(readonly)

    def get(self, k):
        """读取属性.

        Args:
            k: 属性名.

        Returns:
            属性值, 缺失为 None.
        """
        return self.d.get(k)

    def set(self, k, v) -> None:
        """写入属性.

        Args:
            k: 属性名.
            v: 属性值.
        """
        self.d[k] = v


class JsFn:
    """JS 函数值."""

    def __init__(self, fn) -> None:
        """包装 Python 可调用为 JS 函数语义.

        Args:
            fn: 被包装的可调用对象.
        """
        self.fn = fn


class JsBuffer:
    """JS ArrayBuffer."""

    def __init__(self, data: bytearray) -> None:
        """持有底层字节.

        Args:
            data (bytearray): 缓冲区内容.
        """
        self.data = data


class JsU8:
    """JS Uint8Array 视图."""

    def __init__(self, buf: JsBuffer, off: int, length: int) -> None:
        """建立缓冲区视图.

        Args:
            buf (JsBuffer): 底层缓冲区.
            off (int): 起始偏移.
            length (int): 视图长度.
        """
        self.buf = buf
        self.off = off
        self.length = length


class JsExn:
    """JS 异常对象(经 __wbindgen_exn_store 传入 WASM)."""

    def __init__(self, exc: BaseException) -> None:
        """包装 Python 异常为 JS 异常语义.

        Args:
            exc (BaseException): 原始异常.
        """
        self.exc = exc


class Signtool:
    """已加载的 signtool WASM 实例, 提供 ``sign`` 计算 M-S.

    线程安全: 内部以锁串行化 WASM 调用(wasmtime Store 不允许并发进入).
    """

    def __init__(self, wasm: bytes, favicon_url: str) -> None:
        """实例化 WASM 并完成 signtool 初始化.

        Args:
            wasm (bytes): 当前站点 mhs_fe_sign_bg WASM 字节.
            favicon_url (str): 站点 favicon 绝对 URL(参与签名初始化).

        Raises:
            ValueError: WASM 导入出现未知基名或同基名歧义(站点 ABI 变更).
        """
        self._lock = threading.Lock()
        self._heap: list[Any] = [None]  # heap[0] => JS null/undefined
        self._engine = wasmtime.Engine()
        self._store = wasmtime.Store(self._engine)
        module = wasmtime.Module(self._engine, wasm)
        self._favicon = favicon_url
        self._icon_elem = JsObj({})
        self._exports = self._instantiate(module)
        # signtool_new 仅允许调用一次, tool 指针在实例化后立即取得并缓存
        self._tool = self._exports["signtool_new"](self._store)

    # ---- 对象堆 ----

    def _push(self, x: Any) -> int:
        """对象入堆并返回索引.

        Args:
            x: 任意 JS 语义值.

        Returns:
            int: 堆索引.
        """
        self._heap.append(x)
        return len(self._heap) - 1

    def _heap_get(self, idx: int) -> Any:
        """按索引读取堆对象, 越界视为 JS undefined.

        Args:
            idx (int): 堆索引.

        Returns:
            堆对象或 None(undefined).
        """
        return self._heap[idx] if 0 <= idx < len(self._heap) else None

    def _read_str(self, ptr: int, ln: int) -> str:
        """读取 WASM 线性内存中的 UTF-8 字符串.

        Args:
            ptr (int): 起始地址.
            ln (int): 字节长度.

        Returns:
            str: 解码文本.
        """
        return bytes(self._memory.read(self._store, ptr, ptr + ln)).decode("utf-8", "replace")

    def _write_str(self, ptr: int, s: str) -> None:
        """向 WASM 线性内存写入 UTF-8 字符串.

        Args:
            ptr (int): 起始地址.
            s (str): 文本.
        """
        self._memory.write(self._store, s.encode("utf-8"), ptr)

    # ---- 内存分配 ----

    def _malloc(self, ln: int, align: int = 1) -> int:
        """调用 WASM 导出分配器.

        Args:
            ln (int): 字节数.
            align (int): 对齐.

        Returns:
            int: 分配地址.
        """
        return self._exports["__wbindgen_export_1"](self._store, ln, align)

    def _pass_str(self, s: str) -> tuple[int, int]:
        """把字符串写入 WASM 内存, 返回 (指针, 长度).

        Args:
            s (str): 文本.

        Returns:
            tuple[int, int]: (指针, 字节长度).
        """
        b = s.encode("utf-8")
        ptr = self._malloc(len(b), 1)
        self._write_str(ptr, s)
        return ptr, len(b)

    # ---- 导入函数实现(wasm-bindgen ABI 语义) ----

    def _i_string_new(self, p: int, l: int) -> int:
        """__wbindgen_string_new: 从 WASM 内存新建 JS 字符串."""
        return self._push(self._read_str(p, l))

    def _i_throw(self, p: int, l: int) -> None:
        """__wbindgen_throw: WASM 抛错, 直接终止."""
        raise RuntimeError("wasm throw: " + self._read_str(p, l))

    def _i_string_get(self, retptr: int, i: int) -> None:
        """__wbindgen_string_get: 读取堆上字符串写入 retptr(非字符串写空)."""
        v = self._heap[i]
        if isinstance(v, str):
            ptr, ln = self._pass_str(v)
            self._mem_write_i32(retptr, ptr)
            self._mem_write_i32(retptr + 4, ln)
        else:
            self._mem_write_i32(retptr, 0)
            self._mem_write_i32(retptr + 4, 0)

    def _i_debug_string(self, retptr: int, i: int) -> None:
        """__wbindgen_debug_string: 调试描述字符串写入 retptr."""
        v = self._heap[i]
        if v is None:
            s = "undefined"
        elif isinstance(v, str):
            s = f'"{v}"'
        elif isinstance(v, JsObj):
            s = "Object(" + str(v.d.keys()) + ")"
        elif isinstance(v, JsFn):
            s = "Function(f)"
        else:
            s = str(v)
        ptr, ln = self._pass_str(s)
        self._mem_write_i32(retptr, ptr)
        self._mem_write_i32(retptr + 4, ln)

    def _w_get(self, o: int, k: int) -> int:
        """Reflect.get: null/undefined 上抛 TypeError(glue 捕获后存 exn)."""
        target = self._heap_get(o)
        if target is None:
            return self._js_exn(TypeError(f"Reflect.get on null, key={self._heap_get(k)!r}"))
        try:
            v = target.get(self._heap_get(k)) if isinstance(target, JsObj) else None
        except Exception as exc:  # noqa: BLE001 - 模拟 JS 属性访问器异常传导
            return self._js_exn(exc)
        return 0 if v is None else self._push(v)

    def _w_set(self, o: int, k: int, v: int) -> int:
        """Reflect.set: 只读访问器返回 false 且不修改, 其余写入并返回 true."""
        target = self._heap_get(o)
        key = self._heap_get(k)
        if isinstance(target, JsObj) and key in target.readonly:
            return 0
        if target is not None:
            target.set(key, self._heap_get(v))
        return 1

    def _w_set_u8(self, i: int, src_i: int, offset: int) -> None:
        """Uint8Array.set: 拷贝源视图字节到目标视图偏移处."""
        u8, src = self._heap[i], self._heap[src_i]
        data = bytes(src.buf.data[src.off:src.off + src.length])
        u8.buf.data[u8.off + offset:u8.off + offset + src.length] = data

    def _w_call_2(self, fn: int, this: int) -> int:
        """单参函数调用: 结果入堆."""
        return self._push(self._heap[fn].fn(self._heap[this]))

    def _w_call_3(self, fn: int, this: int, arg: int) -> int:
        """双参函数调用: 结果入堆."""
        return self._push(self._heap[fn].fn(self._heap[this], self._heap[arg]))

    def _w_get_random_values(self, fn: int, arr: int) -> None:
        """crypto.getRandomValues: 以 os.urandom 填充视图."""
        u8 = self._heap[arr]
        u8.buf.data[u8.off:u8.off + u8.length] = os.urandom(u8.length)

    def _w_get_attribute(self, retptr: int, elem: int, np: int, nl: int) -> None:
        """Element.getAttribute: 仅实现 favicon href 的读取."""
        name = self._read_str(np, nl)
        value = self._favicon if name == "href" else None
        if value is None:
            self._mem_write_i32(retptr, 0)
            self._mem_write_i32(retptr + 4, 0)
            return
        ptr, ln = self._pass_str(value)
        self._mem_write_i32(retptr, ptr)
        self._mem_write_i32(retptr + 4, ln)

    def _w_query_selector(self, o: int, sp: int, sl: int) -> int:
        """document.querySelector: 仅识别 favicon 选择器."""
        sel = self._read_str(sp, sl)
        if sel == "link[rel*='icon']":
            return self._push(self._icon_elem)
        return 0

    def _w_new_noargs(self, p: int, l: int) -> int:
        """new Function(code)(): 求值结果为全局对象."""
        return self._push(JsFn(lambda this=None: self._global))

    def _acc_global(self) -> int:
        """static accessor GLOBAL/GLOBAL_THIS: 返回全局对象索引."""
        return self._push(self._global)

    def _acc_null(self) -> int:
        """static accessor WINDOW/SELF/PROCESS: 浏览器环境均为 null."""
        return 0

    def _js_exn(self, exc: BaseException) -> int:
        """把异常存入堆并调用 __wbindgen_exn_store, 返回 0.

        Args:
            exc (BaseException): 待传导的异常.

        Returns:
            int: 0(glue 约定的异常分支返回值).
        """
        idx = self._push(JsExn(exc))
        self._exports["__wbindgen_export_0"](self._store, idx)
        return 0

    def _mem_write_i32(self, ptr: int, val: int) -> None:
        """向线性内存写入小端 i32.

        Args:
            ptr (int): 地址.
            val (int): 数值.
        """
        self._memory.write(self._store, int(val).to_bytes(4, "little", signed=True), ptr)

    # ---- 全局对象图 ----

    def _build_globals(self) -> JsObj:
        """构造 glue 环境探测所需的全局对象图.

        Returns:
            JsObj: globalThis 对象(含 crypto/document/navigator).
        """
        crypto_obj = JsObj({"getRandomValues": JsFn(lambda arr: self._w_get_random_values(0, 0))})
        navigator_obj = JsObj({"webdriver": False}, readonly=("webdriver",))
        global_obj = JsObj({
            "crypto": crypto_obj,
            "document": JsObj({}),
            "navigator": navigator_obj,
            "globalThis": None,
        })
        global_obj.d["globalThis"] = global_obj
        return global_obj

    # ---- 导入分派与实例化 ----

    def _resolve_import(self, imp: wasmtime.ImportType):
        """按去 hash 基名与函数签名分派 WASM 导入实现.

        Args:
            imp (wasmtime.ImportType): 导入描述.

        Returns:
            callable: 绑定 self 的 Python 实现.

        Raises:
            ValueError: 基名未知或同基名签名歧义(站点 ABI 变更).
        """
        base = _base_name(imp.name)
        ftype = imp.type
        n_params = len(ftype.params)
        n_results = len(ftype.results)
        if base == "__wbg_set":
            # 两处 set: Reflect.set(返回 i32) 与 Uint8Array.set(无返回)
            if n_results:
                return self._w_set
            return self._w_set_u8
        if base == "__wbg_call":
            # 两处 call: 单参与双参调用
            if n_params == 2:
                return self._w_call_2
            return self._w_call_3
        table: dict[str, Any] = {
            "__wbindgen_string_new": self._i_string_new,
            "__wbindgen_number_new": lambda f: self._push(f),
            "__wbindgen_object_clone_ref": lambda i: self._push(self._heap[i]),
            "__wbindgen_object_drop_ref": lambda i: None,
            "__wbindgen_is_object": lambda i: 1 if isinstance(self._heap[i], JsObj) else 0,
            "__wbindgen_is_string": lambda i: 1 if isinstance(self._heap[i], str) else 0,
            "__wbindgen_is_function": lambda i: 1 if isinstance(self._heap[i], JsFn) else 0,
            "__wbindgen_is_undefined": lambda i: 1 if self._heap[i] is None else 0,
            "__wbindgen_boolean_get": lambda i: (
                1 if self._heap[i] is True else (0 if self._heap[i] is False else 2)
            ),
            "__wbindgen_memory": lambda: self._push(JsObj({"__memory__": True})),
            "__wbindgen_throw": self._i_throw,
            "__wbindgen_debug_string": self._i_debug_string,
            "__wbindgen_string_get": self._i_string_get,
            "__wbg_get": self._w_get,
            "__wbg_newwithlength": lambda l: self._push(JsU8(JsBuffer(bytearray(l)), 0, l)),
            "__wbg_new": lambda i: self._push(JsU8(self._heap[i], 0, len(self._heap[i].data))),
            "__wbg_newwithbyteoffsetandlength": lambda i, off, ln: self._push(
                JsU8(self._heap[i], off, ln)
            ),
            "__wbg_buffer": lambda i: self._push(
                self._heap[i].buf if isinstance(self._heap[i], JsU8)
                else JsBuffer(bytearray(self._memory.data_len(self._store)))
            ),
            "__wbg_subarray": lambda i, s, e: self._push(
                JsU8(self._heap[i].buf, self._heap[i].off + s, e - s)
            ),
            "__wbg_getRandomValues": self._w_get_random_values,
            "__wbg_randomFillSync": lambda fn, arr: None,
            "__wbg_getAttribute": self._w_get_attribute,
            "__wbg_querySelector": self._w_query_selector,
            "__wbg_instanceof_Window": lambda i: 1 if self._heap[i] is self._global else 0,
            "__wbg_document": lambda o: self._push(self._global.get("document")),
            "__wbg_navigator": lambda o: self._push(self._global.get("navigator")),
            "__wbg_crypto": lambda o: self._push(self._global.get("crypto")),
            "__wbg_process": self._acc_null,
            "__wbg_versions": self._acc_null,
            "__wbg_node": self._acc_null,
            "__wbg_msCrypto": self._acc_null,
            "__wbg_require": self._acc_null,
            "__wbg_newnoargs": self._w_new_noargs,
            "__wbg_static_accessor_GLOBAL": self._acc_global,
            "__wbg_static_accessor_GLOBAL_THIS": self._acc_global,
            "__wbg_static_accessor_WINDOW": self._acc_null,
            "__wbg_static_accessor_SELF": self._acc_null,
            "__wbg_static_accessor_PROCESS": self._acc_null,
        }
        impl = table.get(base)
        if impl is None:
            raise ValueError(f"未知的 WASM 导入: {imp.name} (站点 ABI 可能已变更)")
        return impl

    def _instantiate(self, module: wasmtime.Module) -> wasmtime.InstanceExports:
        """绑定导入并实例化 WASM.

        Args:
            module (wasmtime.Module): 已编译模块.

        Returns:
            wasmtime.InstanceExports: 导出表.

        Raises:
            ValueError: 导入基名歧义.
        """
        self._global = self._build_globals()
        imports = []
        for imp in module.imports:
            imports.append(wasmtime.Func(self._store, imp.type, self._resolve_import(imp)))
        inst = wasmtime.Instance(self._store, module, imports)
        exp = inst.exports(self._store)
        self._memory = exp["memory"]
        return exp

    # ---- 对外 API ----

    def sign(self, url: str, ts: int) -> str:
        """计算 M-S 签名.

        Args:
            url (str): 签名输入, 请求纯相对路径(不含 query).
            ts (int): Unix 秒时间戳, 与 M-T 一致.

        Returns:
            str: M-S 签名值.

        Raises:
            RuntimeError: WASM 内部抛错(结果标记位 flag 非 0).
        """
        with self._lock:
            retptr = self._exports["__wbindgen_add_to_stack_pointer"](self._store, -16)
            ptr, ln = self._pass_str(url)
            self._exports["signtool_sign"](self._store, retptr, self._tool, ptr, ln, ts)
            vals = bytes(self._memory.read(self._store, retptr, retptr + 16))
            out_ptr = int.from_bytes(vals[0:4], "little", signed=True)
            out_len = int.from_bytes(vals[4:8], "little", signed=True)
            flag = int.from_bytes(vals[12:16], "little", signed=True)
            result = self._read_str(out_ptr, out_len) if not flag else None
            self._exports["__wbindgen_export_3"](self._store, out_ptr, out_len, 1)
            self._exports["__wbindgen_add_to_stack_pointer"](self._store, 16)
        if result is None:
            raise RuntimeError("signtool_sign 未返回签名(站点 ABI 可能已变更)")
        return result


async def _discover() -> Signtool:
    """从站点动态发现当前 WASM 与 favicon 并加载 Signtool.

    发现链: 站点 HTML -> favicon 链接与 glue chunk(http.*.js) 地址 ->
    glue 内的 mhs_fe_sign_bg.*.wasm 地址 -> 下载字节加载.

    Returns:
        Signtool: 可用的签名器.

    Raises:
        ValueError: 任一环节未在站点资源中找到对应引用.
    """
    async with HttpClient() as client:
        html = (await client.get(_SITE + "/")).text
        m_icon = _ICON_RE.search(html)
        if m_icon is None:
            raise ValueError("站点页面未找到 favicon 链接, 无法初始化签名器")
        chunks = list(dict.fromkeys(_HTTP_CHUNK_RE.findall(html)))
        if not chunks:
            raise ValueError("站点页面未找到 glue chunk 引用, 无法定位签名 WASM")
        for chunk in chunks:
            js = (await client.get(chunk)).text
            m_wasm = _WASM_RE.search(js)
            if m_wasm is not None:
                wasm_url = m_wasm.group(0)
                break
        else:
            raise ValueError("glue chunk 中未找到 mhs_fe_sign_bg WASM 引用")
        wasm = (await client.get(wasm_url)).content
    return Signtool(wasm, m_icon.group(1))


_tool: Signtool | None = None
_tool_lock = asyncio.Lock()
_ts_lock = threading.Lock()
_last_ts: dict[str, int] = {}


async def ensure_tool() -> Signtool:
    """确保签名器已加载(进程内惰性初始化, 可并发调用).

    Returns:
        Signtool: 全局签名器单例.
    """
    global _tool
    if _tool is None:
        async with _tool_lock:
            if _tool is None:
                _tool = await _discover()
    return _tool


def signed_headers(path: str) -> dict[str, str]:
    """为一条 API 请求构建 M-S/M-T/Web-Version 请求头.

    时间戳按签名路径(纯路径, 不含 query)严格递增, 满足服务端重放检测.

    Args:
        path (str): 请求相对路径, 可含 query.

    Returns:
        dict[str, str]: 三个签名相关请求头.

    Callers:
        - ``services/online_sources/mihuashi/client.py:api_get``
    """
    tool = _tool
    if tool is None:
        raise RuntimeError("签名器尚未初始化, 请先调用 ensure_tool")
    sign_path = path.split("?", 1)[0]
    with _ts_lock:
        ts = max(int(time.time()), _last_ts.get(sign_path, 0) + 1)
        _last_ts[sign_path] = ts
    return {
        "M-S": tool.sign(sign_path, ts),
        "M-T": str(ts),
        "Web-Version": "frontend",
    }
