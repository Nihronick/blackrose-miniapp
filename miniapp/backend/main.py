import os
import re
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

sys.path.append(str(Path(__file__).parent))

from guides import CONTENT, MAIN_CATEGORIES, SUBMENUS

try:
    from icons import ALL_ICONS, get_icon

    ICONS_AVAILABLE = True
except ImportError:
    ICONS_AVAILABLE = False
    ALL_ICONS = {}
    print("WARNING: icons.py not found - icons will not be displayed")

app = FastAPI(title="BlackRose Mini App API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

guide_stats: dict[str, int] = {}


def resolve_icon(icon_raw: str | None) -> str | None:
    """Конвертирует имя иконки в URL"""
    if not icon_raw or not ICONS_AVAILABLE:
        return None
    if icon_raw.startswith("http"):
        return icon_raw
    if icon_raw in ALL_ICONS:
        return get_icon(icon_raw)
    return None


def format_guide_text(text: str) -> str:
    """
    Форматирует текст гайда в HTML:
    - {{icon_name}} → <img> (inline)
    - **text** → <strong> (inline, НЕ block)
    - Переносы строк → <br>
    """
    if not text:
        return ""

    html_content = text

    # 1. Заменяем {{icon_name}} на inline <img>
    if ICONS_AVAILABLE:

        def replace_icon(match):
            icon_name = match.group(1).strip()
            try:
                icon_url = get_icon(icon_name)
                if icon_url and not icon_url.endswith("default.png"):
                    return (
                        f'<img src="{icon_url}" alt="{icon_name}" '
                        f'class="inline-icon" '
                        f"onerror=\'this.style.display='none'\'>"
                    )
            except Exception:
                pass
            return ""

        html_content = re.sub(r"\{\{(\w+(?:\s+\w+)*)\}\}", replace_icon, html_content)
    else:
        # Убираем плейсхолдеры если иконки недоступны
        html_content = re.sub(r"\{\{(\w+(?:\s+\w+)*)\}\}", "", html_content)

    # 2. **text** → <strong> (inline)
    html_content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html_content)

    # 3. Переносы строк
    html_content = html_content.replace("\n", "<br>")

    # 4. Убираем лишние <br> подряд (больше 2)
    html_content = re.sub(r"(<br>\s*){3,}", "<br><br>", html_content)

    return html_content


# ═══════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "BlackRose Mini App API",
        "icons_available": ICONS_AVAILABLE,
        "total_icons": len(ALL_ICONS),
    }


@app.get("/api/categories")
def get_categories():
    categories = []
    for key, data in MAIN_CATEGORIES.items():
        title = data["title"] if isinstance(data, dict) else data
        icon_raw = data.get("icon") if isinstance(data, dict) else None

        categories.append(
            {"key": key, "title": title, "icon": resolve_icon(icon_raw), "count": len(SUBMENUS.get(key, []))}
        )
    return {"categories": categories}


@app.get("/api/category/{category_key}")
async def get_category(category_key: str):
    if category_key not in SUBMENUS:
        raise HTTPException(404, detail="Category not found")

    items = []
    for item in SUBMENUS[category_key]:
        if len(item) >= 3:
            key, title, icon_raw = item[0], item[1], item[2]
        else:
            key, title = item[0], item[1]
            icon_raw = None

        guide = CONTENT.get(key, {})

        # Чистое превью без {{icon}} плейсхолдеров
        raw_preview = guide.get("text", "")[:150]
        clean_preview = re.sub(r"\{\{.*?\}\}", "", raw_preview).strip()
        clean_preview = re.sub(r"\*\*(.+?)\*\*", r"\1", clean_preview)

        items.append(
            {
                "key": key,
                "title": title,
                "icon": resolve_icon(icon_raw),
                "preview": clean_preview + "..." if clean_preview else "",
                "has_photo": bool(guide.get("photo")),
            }
        )

    cat_data = MAIN_CATEGORIES.get(category_key, {})
    category_title = cat_data["title"] if isinstance(cat_data, dict) else cat_data

    return {"category": {"key": category_key, "title": category_title}, "items": items}


@app.get("/api/guide/{guide_key}")
async def get_guide(guide_key: str):
    guide = CONTENT.get(guide_key)
    if not guide:
        raise HTTPException(404, detail="Guide not found")

    guide_stats[guide_key] = guide_stats.get(guide_key, 0) + 1

    # Фото
    photo = guide.get("photo")
    photo_list = []
    if photo:
        if isinstance(photo, list):
            photo_list = [p for p in photo if p and str(p) not in ("None", "")]
        elif isinstance(photo, str) and photo not in ("None", ""):
            photo_list = [photo]

    # Форматируем текст с иконками
    raw_text = guide.get("text", "")
    formatted_text = format_guide_text(raw_text)

    icon_raw = guide.get("icon")

    return {
        "key": guide_key,
        "title": guide.get("title", guide_key),
        "icon": resolve_icon(icon_raw),
        "text": formatted_text,
        "photo": photo_list,
        "views": guide_stats[guide_key],
    }


@app.get("/api/search")
async def search_guides(q: str = Query(min_length=2)):
    query = q.lower().strip()
    results = []

    for key, guide in CONTENT.items():
        title = guide.get("title", key)
        text = guide.get("text", "").lower()

        if query in key.lower() or query in title.lower() or query in text:
            icon_raw = guide.get("icon")
            raw_preview = guide.get("text", "")[:150]
            clean_preview = re.sub(r"\{\{.*?\}\}", "", raw_preview).strip()

            results.append(
                {
                    "key": key,
                    "title": title,
                    "icon": resolve_icon(icon_raw),
                    "preview": clean_preview + "...",
                }
            )

    return {"results": results[:10]}


# Frontend
frontend_dir = Path(__file__).parent.parent / "frontend"


@app.get("/frontend")
async def serve_index():
    index_path = frontend_dir / "index.html"
    if not index_path.exists():
        return {"error": "index.html not found", "path": str(index_path)}
    return FileResponse(index_path, media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")

    print(f"Starting on {host}:{port}")
    print(f"Icons: {len(ALL_ICONS)} loaded" if ICONS_AVAILABLE else "No icons")

    uvicorn.run("main:app", host=host, port=port, reload=os.getenv("RAILWAY_ENVIRONMENT") is None)
