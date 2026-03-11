"""
BlackRose Mini App API
"""
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote

from fastapi import FastAPI, HTTPException, Query, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

# ═══════════════════════════════════════════════════════
# ЛОГИРОВАНИЕ
# ═══════════════════════════════════════════════════════
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("blackrose")

# Подавляем шум uvicorn в продакшене
if os.getenv("RAILWAY_ENVIRONMENT"):
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

sys.path.append(str(Path(__file__).parent))

from guides import CONTENT, MAIN_CATEGORIES, SUBMENUS

try:
    from icons import ALL_ICONS, get_icon
    ICONS_AVAILABLE = True
    logger.info(f"Icons loaded: {len(ALL_ICONS)}")
except ImportError:
    ICONS_AVAILABLE = False
    ALL_ICONS = {}
    logger.warning("icons.py not found — icons disabled")


# ═══════════════════════════════════════════════════════
# TELEGRAM INIT DATA VERIFICATION
# ═══════════════════════════════════════════════════════
# Только BOT_TOKEN — никаких списков пользователей
# Бот сам решает кому показывать кнопку MiniApp
# Здесь мы только проверяем что запрос реально из Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Максимальный возраст initData (секунды). 0 = без ограничения
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))

if not BOT_TOKEN:
    logger.warning(
        "BOT_TOKEN not set! "
        "initData verification DISABLED — anyone can access the API"
    )


def verify_telegram_init_data(init_data: str) -> dict | None:
    """
    Проверяет HMAC-подпись Telegram initData.
    
    Не проверяет whitelist — только что данные подписаны Telegram
    через наш бот. Если подпись верна = запрос из Telegram.
    
    Returns:
        dict с данными пользователя или None
    
    Docs: https://core.telegram.org/bots/webapps#validating-data
    """
    if not BOT_TOKEN:
        return {"id": 0, "first_name": "Guest"}
    
    if not init_data:
        return None

    try:
        parsed = parse_qs(init_data, keep_blank_values=True)
        
        received_hash = parsed.get("hash", [None])[0]
        if not received_hash:
            logger.debug("initData: no hash field")
            return None

        # data_check_string — все поля кроме hash, 
        # отсортированные по ключу
        check_pairs = []
        for key, values in sorted(parsed.items()):
            if key == "hash":
                continue
            check_pairs.append(f"{key}={values[0]}")
        data_check_string = "\n".join(check_pairs)

        # secret_key = HMAC_SHA256("WebAppData", bot_token)
        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        # calculated_hash = HMAC_SHA256(secret_key, data_check_string)
        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            logger.warning("initData: HMAC mismatch — forged request?")
            return None

        # Проверяем свежесть (auth_date)
        if INIT_DATA_MAX_AGE > 0:
            auth_date_str = parsed.get("auth_date", [None])[0]
            if auth_date_str:
                age = time.time() - int(auth_date_str)
                if age > INIT_DATA_MAX_AGE:
                    logger.warning(
                        f"initData: expired ({age:.0f}s > {INIT_DATA_MAX_AGE}s)"
                    )
                    return None

        # Парсим user
        user_raw = parsed.get("user", [None])[0]
        if user_raw:
            user = json.loads(unquote(user_raw))
            logger.debug(
                f"initData OK: user {user.get('id')} "
                f"({user.get('first_name', '?')})"
            )
            return user

        logger.debug("initData: no user field")
        return None

    except Exception as e:
        logger.error(f"initData verification error: {e}")
        return None


async def require_telegram_user(request: Request) -> dict:
    """
    FastAPI Dependency.
    
    Проверяет что запрос пришёл из Telegram WebApp
    с валидной подписью нашего бота.
    
    НЕ проверяет whitelist — это делает бот,
    который решает кому показать кнопку.
    """
    if not BOT_TOKEN:
        # Без токена пускаем всех (dev-режим)
        return {"id": 0, "first_name": "Dev"}

    # initData из заголовка или query-параметра
    init_data = (
        request.headers.get("X-Telegram-Init-Data", "")
        or request.query_params.get("initData", "")
    )

    if not init_data:
        raise HTTPException(
            status_code=403,
            detail="Откройте приложение через Telegram бота",
        )

    user = verify_telegram_init_data(init_data)
    if user is None:
        raise HTTPException(
            status_code=403,
            detail="Неверная авторизация Telegram. Откройте заново через бота.",
        )

    return user


# ═══════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════
app = FastAPI(title="BlackRose Mini App API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*", "X-Telegram-Init-Data"],
)

guide_stats: dict[str, int] = {}


# ═══════════════════════════════════════════════════════
# REQUEST LOGGING MIDDLEWARE
# ═══════════════════════════════════════════════════════
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration_ms = (time.time() - start) * 1000

    # Не логируем фронтенд и health-check
    path = request.url.path
    if not path.startswith("/frontend") and path != "/":
        logger.info(
            f"{request.method} {path} → "
            f"{response.status_code} ({duration_ms:.1f}ms)"
        )
    return response


# ═══════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════
def resolve_icon(icon_raw: str | None) -> str | None:
    if not icon_raw or not ICONS_AVAILABLE:
        return None
    if icon_raw.startswith("http"):
        return icon_raw
    if icon_raw in ALL_ICONS:
        return get_icon(icon_raw)
    return None


def format_guide_text(text: str) -> str:
    """
    {{icon_name}} → <img>
    **text** → <strong>
    *text* → <em>
    \\n → <br>
    """
    if not text:
        return ""

    html = text

    # 1. Иконки
    if ICONS_AVAILABLE:
        def replace_icon(match):
            name = match.group(1).strip()
            try:
                url = get_icon(name)
                if url and not url.endswith("default.png"):
                    return (
                        f'<img src="{url}" alt="{name}" '
                        f'class="inline-icon" '
                        f'onerror="this.style.display=\'none\'">'
                    )
            except Exception:
                pass
            return ""
        html = re.sub(r"\{\{(\w+(?:\s+\w+)*)\}\}", replace_icon, html)
    else:
        html = re.sub(r"\{\{(\w+(?:\s+\w+)*)\}\}", "", html)

    # 2. Bold
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    # 3. Italic
    html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html)
    # 4. Newlines
    html = html.replace("\n", "<br>")
    # 5. Cleanup
    html = re.sub(r"(<br>\s*){3,}", "<br><br>", html)

    return html


def _extract_media_list(media) -> list[str]:
    """Извлекает список URL из поля медиа (photo/video/document)."""
    if not media:
        return []
    if isinstance(media, list):
        return [
            item for item in media
            if item and isinstance(item, str) and item not in ("None", "")
        ]
    if isinstance(media, str) and media not in ("None", ""):
        return [media]
    return []


# ═══════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════
@app.get("/")
async def root():
    """Health check — без авторизации."""
    return {
        "status": "ok",
        "service": "BlackRose Mini App API",
        "version": "2.0.0",
        "icons_available": ICONS_AVAILABLE,
        "total_icons": len(ALL_ICONS),
        "auth_required": bool(BOT_TOKEN),
    }


@app.get("/api/auth")
async def check_auth(user: dict = Depends(require_telegram_user)):
    """Проверка авторизации — фронтенд вызывает первым."""
    return {
        "authorized": True,
        "user_id": user.get("id"),
        "first_name": user.get("first_name", ""),
    }


@app.get("/api/categories")
def get_categories(user: dict = Depends(require_telegram_user)):
    categories = []
    for key, data in MAIN_CATEGORIES.items():
        title = data["title"] if isinstance(data, dict) else data
        icon_raw = data.get("icon") if isinstance(data, dict) else None
        categories.append({
            "key": key,
            "title": title,
            "icon": resolve_icon(icon_raw),
            "count": len(SUBMENUS.get(key, [])),
        })
    return {"categories": categories}


@app.get("/api/category/{category_key}")
async def get_category(
    category_key: str,
    user: dict = Depends(require_telegram_user),
):
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
        raw_preview = guide.get("text", "")[:150]
        clean_preview = re.sub(r"\{\{.*?\}\}", "", raw_preview).strip()
        clean_preview = re.sub(r"\*\*(.+?)\*\*", r"\1", clean_preview)

        items.append({
            "key": key,
            "title": title,
            "icon": resolve_icon(icon_raw),
            "preview": (clean_preview + "...") if clean_preview else "",
            "has_photo": bool(guide.get("photo")),
            "has_video": bool(guide.get("video")),
            "has_document": bool(guide.get("document")),
        })

    cat_data = MAIN_CATEGORIES.get(category_key, {})
    category_title = (
        cat_data["title"] if isinstance(cat_data, dict) else cat_data
    )
    return {
        "category": {"key": category_key, "title": category_title},
        "items": items,
    }

@app.get("/api/debug")
async def debug_auth(request: Request):
    """Временный — УДАЛИТЬ после отладки!"""
    init_data = (
        request.headers.get("X-Telegram-Init-Data", "")
        or request.query_params.get("initData", "")
    )
    
    result = {
        "has_bot_token": bool(BOT_TOKEN),
        "bot_token_start": BOT_TOKEN[:10] + "..." if BOT_TOKEN else "EMPTY",
        "has_init_data": bool(init_data),
        "init_data_length": len(init_data) if init_data else 0,
    }
    
    if init_data and BOT_TOKEN:
        # Детальная проверка
        try:
            parsed = parse_qs(init_data, keep_blank_values=True)
            result["parsed_keys"] = list(parsed.keys())
            result["has_hash"] = "hash" in parsed
            result["has_user"] = "user" in parsed
            
            auth_date = parsed.get("auth_date", [None])[0]
            if auth_date:
                import time as _time
                age = _time.time() - int(auth_date)
                result["auth_date_age_seconds"] = round(age)
            
            # Попробуем верифицировать
            user = verify_telegram_init_data(init_data)
            result["verified"] = user is not None
            if user:
                result["user_id"] = user.get("id")
                result["user_name"] = user.get("first_name")
            else:
                result["verify_error"] = "HMAC mismatch or expired"
                
        except Exception as e:
            result["error"] = str(e)
    
    return result


@app.get("/api/guide/{guide_key}")
async def get_guide(
    guide_key: str,
    user: dict = Depends(require_telegram_user),
):
    guide = CONTENT.get(guide_key)
    if not guide:
        raise HTTPException(404, detail="Guide not found")

    guide_stats[guide_key] = guide_stats.get(guide_key, 0) + 1
    logger.debug(f"Guide viewed: {guide_key} (views: {guide_stats[guide_key]})")

    return {
        "key": guide_key,
        "title": guide.get("title", guide_key),
        "icon": resolve_icon(guide.get("icon")),
        "text": format_guide_text(guide.get("text", "")),
        "photo": _extract_media_list(guide.get("photo")),
        "video": _extract_media_list(guide.get("video")),
        "document": _extract_media_list(guide.get("document")),
        "views": guide_stats[guide_key],
    }


@app.get("/api/search")
async def search_guides(
    q: str = Query(min_length=2),
    user: dict = Depends(require_telegram_user),
):
    query = q.lower().strip()
    results = []

    for key, guide in CONTENT.items():
        title = guide.get("title", key)
        text = guide.get("text", "").lower()

        if query in key.lower() or query in title.lower() or query in text:
            raw_preview = guide.get("text", "")[:150]
            clean_preview = re.sub(r"\{\{.*?\}\}", "", raw_preview).strip()
            results.append({
                "key": key,
                "title": title,
                "icon": resolve_icon(guide.get("icon")),
                "preview": clean_preview + "...",
            })

    return {"results": results[:10]}


@app.get("/api/stats")
async def get_api_stats():
    """Публичная статистика — без авторизации."""
    return {
        "total_guides": len(CONTENT),
        "total_categories": len(MAIN_CATEGORIES),
        "total_views": sum(guide_stats.values()),
    }


# ═══════════════════════════════════════════════════════
# FRONTEND
# ═══════════════════════════════════════════════════════
frontend_dir = Path(__file__).parent / "frontend"


@app.get("/frontend")
async def serve_index():
    index_path = frontend_dir / "index.html"
    if not index_path.exists():
        logger.error(f"index.html not found: {index_path}")
        return JSONResponse({"error": "index.html not found"}, status_code=404)
    return FileResponse(index_path, media_type="text/html")


# ═══════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════
@app.on_event("startup")
async def startup():
    logger.info("=" * 50)
    logger.info("BlackRose Mini App API v2.0.0")
    logger.info(f"  Icons:      {len(ALL_ICONS)}" if ICONS_AVAILABLE else "  Icons:      disabled")
    logger.info(f"  Guides:     {len(CONTENT)}")
    logger.info(f"  Categories: {len(MAIN_CATEGORIES)}")
    logger.info(f"  Auth:       {'BOT_TOKEN set' if BOT_TOKEN else 'OPEN (no token)'}")
    logger.info(f"  Max age:    {INIT_DATA_MAX_AGE}s")
    logger.info("=" * 50)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    logger.info(f"Starting on {host}:{port}")

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=os.getenv("RAILWAY_ENVIRONMENT") is None,
        log_level="info",
    )