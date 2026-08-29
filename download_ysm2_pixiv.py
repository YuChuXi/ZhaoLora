"""临时脚本: 批量下载指定 pixiv 画师全部作品到 datasets/YSM2.

一次性工具: 对每个画师经 ``profile/all`` 拉取全部作品并按页展开, 并发下载原图到
``datasets/YSM2`` 平铺目录 (pixiv 作品 ID 全局唯一, 无命名冲突)。认证经
``web_annotator/.env`` 的 ``PIXIV_COOKIES`` 提供, 搜索/下载复用项目 PixivSource。

用法 (仓库根):
    .venv/bin/python download_ysm2_pixiv.py

下载完成后删除本脚本。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent
_APP = _BASE / "web_annotator"
sys.path.insert(0, str(_APP))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_APP / ".env")

# .env 中 PIXIV_COOKIES 为相对 web_annotator/ 的路径, 此处锚定后再回写,
# 使 pixiv client 不经 server 也能读到正确 cookie 文件.
_cookies = os.environ.get("PIXIV_COOKIES", "").strip()
if _cookies:
    p = Path(_cookies)
    if not p.is_absolute():
        p = _APP / p
    os.environ["PIXIV_COOKIES"] = str(p)

from services.online_sources import download_post, search_posts  # noqa: E402

# pixiv 画师 ID (对应 https://www.pixiv.net/users/{id})
USER_IDS: list[str] = [
    "23103226",
    "74968020",
    "107665493",
    "110234951",
    "79947518",
    "86654838",
    "90067374",
    "125004459",
]

# 输出目录: datasets/YSM2/main (标准工作子目录, 与任务布局约定一致)
OUT_DIR: Path = _BASE / "datasets" / "YSM2" / "main"

# 并发下载数, 兼顾 pixiv 反爬限流
CONCURRENCY: int = 1


async def download_user(user_id: str) -> tuple[int, int]:
    """下载单个画师的全部作品.

    Args:
        user_id (str): pixiv 画师 ID.

    Returns:
        tuple[int, int]: (成功数, 失败数)。

    Callers:
        - ``download_ysm2_pixiv.py:main``
    """
    ok = 0
    fail = 0
    sem = asyncio.Semaphore(CONCURRENCY)

    async def fetch(post) -> None:
        """并发下载单张作品, 失败记录后继续.

        Args:
            post: 搜索结果帖对象, 含 ``id``/``filename``。

        Returns:
            None: 结果累计进闭包计数.
        """
        nonlocal ok, fail
        async with sem:
            try:
                await download_post("pixiv", post, OUT_DIR)
                ok += 1
            except Exception as exc:
                fail += 1
                print(f"  失败 {post.id}: {exc}")

    page = 1
    while True:
        posts, has_more = await search_posts(
            "pixiv", user_id, page=page, extra="mode:user",
        )
        if posts:
            await asyncio.gather(*[fetch(p) for p in posts])
        print(f"画师 {user_id} 第 {page} 页: 本页 {len(posts)} 作品, 累计成功 {ok} 失败 {fail}")
        if not has_more:
            break
        page += 1
    return ok, fail


async def main() -> None:
    """遍历画师列表执行下载并输出汇总.

    Returns:
        None: 下载结果输出到 stdout.

    Callers:
        - ``download_ysm2_pixiv.py:__main__``
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    total_ok = 0
    total_fail = 0
    for uid in USER_IDS:
        print(f"== 开始画师 {uid} ==")
        ok, fail = await download_user(uid)
        total_ok += ok
        total_fail += fail
        print(f"== 画师 {uid} 完成: 成功 {ok} 失败 {fail} ==")
    print(f"全部完成: 成功 {total_ok}, 失败 {total_fail}, 输出目录 {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
