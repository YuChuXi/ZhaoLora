"""文档：渲染 web_annotator/docs/*.md。"""

from __future__ import annotations

import re
from pathlib import Path

import markdown
from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["docs"])

_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"

_DOC_MAP: dict[str, str] = {
    "": "README.md",
    "index": "README.md",
    "readme": "README.md",
    "layout": "layout.md",
    "aesthetic": "aesthetic.md",
    "filter": "filter.md",
    "dedupe": "dedupe.md",
    "crop": "crop.md",
    "paint": "paint.md",
    "tags": "tags.md",
    "tag-clean": "tag-clean.md",
    "ready": "ready.md",
}

_NAV: list[tuple[str, str]] = [
    ("readme", "概览"),
    ("layout", "目录约定"),
    ("aesthetic", "美学"),
    ("filter", "筛选"),
    ("dedupe", "去重"),
    ("crop", "裁剪"),
    ("paint", "蒙版"),
    ("tags", "标签"),
    ("tag-clean", "清洗"),
    ("ready", "就绪"),
]

_MD_LINK = re.compile(r"\]\(([\w\-]+)\.md(#[^)]*)?\)")


def docs_nav() -> list[dict[str, str]]:
    """引用：GET /api/docs；home.js。"""
    return [{"slug": s, "label": lab} for s, lab in _NAV]


def resolve_doc(slug: str) -> Path:
    """引用：get_doc。"""
    key = (slug or "").strip().lower().lstrip("/")
    name = _DOC_MAP.get(key)
    if not name:
        raise HTTPException(404, f"文档不存在: {slug}")
    path = (_DOCS_DIR / name).resolve()
    if not str(path).startswith(str(_DOCS_DIR.resolve())) or not path.is_file():
        raise HTTPException(404, f"文档不存在: {slug}")
    return path


def render_markdown(text: str) -> str:
    """MD → HTML；模块间 .md 链接改为 ?doc=。引用：get_doc。"""
    text = _MD_LINK.sub(r"](?doc=\1\2)", text)
    return markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists"],
        output_format="html5",
    )


@router.get("/api/docs")
def list_docs():
    """引用：home.js。"""
    return {"nav": docs_nav()}


@router.get("/api/docs/{slug}")
def get_doc(slug: str = "readme"):
    """引用：home.js。"""
    path = resolve_doc(slug)
    out_slug = "readme" if slug in ("readme", "index", "") else slug
    raw = path.read_text(encoding="utf-8")
    return {
        "slug": out_slug,
        "file": path.name,
        "title": path.stem,
        "html": render_markdown(raw),
    }
