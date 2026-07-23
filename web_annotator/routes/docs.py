"""文档：渲染 web_annotator/docs/*.md。"""

from __future__ import annotations

import re
from pathlib import Path

import markdown
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse

router = APIRouter(tags=["docs"])

_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"

# slug → 文件名
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
    ("", "概览"),
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
    return [{"slug": s, "label": lab} for s, lab in _NAV]


def resolve_doc(slug: str) -> Path:
    key = (slug or "").strip().lower().lstrip("/")
    name = _DOC_MAP.get(key)
    if not name:
        raise HTTPException(404, f"文档不存在: {slug}")
    path = (_DOCS_DIR / name).resolve()
    if not str(path).startswith(str(_DOCS_DIR.resolve())) or not path.is_file():
        raise HTTPException(404, f"文档不存在: {slug}")
    return path


def render_markdown(text: str) -> str:
    """MD → HTML；把模块间 .md 链接改成首页 ?doc= 查询。"""
    text = _MD_LINK.sub(r"](?doc=\1\2)", text)
    return markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists"],
        output_format="html5",
    )


@router.get("/api/docs")
def list_docs():
    return {"nav": docs_nav(), "dir": str(_DOCS_DIR)}


@router.get("/api/docs/{slug}")
def get_doc(slug: str = "readme"):
    path = resolve_doc("" if slug in ("readme", "index") else slug)
    raw = path.read_text(encoding="utf-8")
    return {
        "slug": "" if slug in ("readme", "index") else slug,
        "file": path.name,
        "title": path.stem,
        "html": render_markdown(raw),
    }


@router.get("/docs/{name}.md")
def raw_markdown(name: str):
    """原始 Markdown 文件（便于外链或本地查看）。"""
    key = name.strip().lower()
    if key == "readme":
        path = resolve_doc("")
    else:
        path = resolve_doc(key)
    return PlainTextResponse(
        path.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
    )
