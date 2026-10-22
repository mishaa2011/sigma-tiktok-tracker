import os
import re
import hmac
import hashlib
import sqlite3
import json
from urllib.request import Request as URLRequest, urlopen
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from contextlib import contextmanager

from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(APP_DIR, "tracker.db"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SOURCEVINE_API_KEY = os.getenv("SOURCEVINE_API_KEY", "").strip()
SOURCEVINE_API_URL = "https://api.sourcevine.io/v1/tiktok/stats"
TZ = timezone(timedelta(hours=3))

if not ADMIN_PASSWORD:
    print("WARNING: Set ADMIN_PASSWORD in your environment before using the admin panel.")
if len(SESSION_SECRET) < 32:
    print("WARNING: Set SESSION_SECRET to a random value of at least 32 characters.")

app = FastAPI(title="TikTok Weekly Tracker")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET or "unsafe-dev-only-change-me", https_only=False, same_site="lax")
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")

def now_local():
    return datetime.now(TZ)

def week_start(dt=None):
    dt = dt or now_local()
    dt = dt.astimezone(TZ)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=midnight.weekday())

def week_key(dt=None):
    return week_start(dt).date().isoformat()

def submission_open(dt=None):
    dt = (dt or now_local()).astimezone(TZ)
    start = week_start(dt)
    end = start + timedelta(days=7)
    return start <= dt < end

def valid_tiktok_url(url):
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        return parsed.scheme in ("http", "https") and (
            host == "tiktok.com" or host.endswith(".tiktok.com")
        ) and ("/video/" in parsed.path or host in ("vm.tiktok.com", "vt.tiktok.com"))
    except Exception:
        return False

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_nick TEXT NOT NULL,
            tiktok_nick TEXT NOT NULL,
            url TEXT NOT NULL,
            canonical_url TEXT NOT NULL UNIQUE,
            week TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            views INTEGER,
            likes INTEGER,
            comments INTEGER,
            shares INTEGER,
            stats_status TEXT NOT NULL DEFAULT 'pending',
            ai_flag TEXT NOT NULL DEFAULT 'not_checked',
            ai_reason TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT ''
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        defaults = {
            "site_name": "SIGMA • TikTok Tracker",
            "min_views": "2000",
            "max_views": "1000000",
            "usd_per_1000": "2",
            "public_domain": "",
            "week_timezone": "Europe/Moscow",
            "submission_enabled": "1"
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))

init_db()

def settings():
    with db() as c:
        rows = c.execute("SELECT key,value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

def admin_required(request: Request):
    if not request.session.get("is_admin"):
        raise HTTPException(status_code=401, detail="Admin login required")

def canonicalize(url):
    # Duplicate check is intentionally conservative: trim spaces and remove query/fragment.
    p = urlparse(url.strip())
    path = p.path.rstrip("/")
    return f"https://{(p.hostname or '').lower()}{path}"


def fetch_tiktok_stats(url: str) -> dict:
    """Fetch one snapshot of public TikTok video stats from Sourcevine."""
    query = urlencode({"url": url})
    req = URLRequest(
        f"{SOURCEVINE_API_URL}?{query}",
        headers={"Authorization": f"Bearer {SOURCEVINE_API_KEY}", "Accept": "application/json"},
        method="GET",
    )
    with urlopen(req, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("success") or not payload.get("available"):
        raise ValueError("Sourcevine сообщает, что статистика недоступна")
    data = payload.get("data") or {}
    result = {}
    for key in ("views", "likes", "comments", "shares"):
        value = data.get(key)
        if value is None:
            raise ValueError(f"В ответе Sourcevine отсутствует поле {key}")
        result[key] = max(0, int(value))
    return result

def calculate_status(row, cfg):
    if row["views"] is None:
        return "pending"
    minimum = int(cfg.get("min_views", "2000"))
    maximum = int(cfg.get("max_views", "1000000"))
    if row["views"] < minimum:
        return "below_minimum"
    if row["views"] > maximum:
        return "over_maximum"
    return "qualified"

@app.get("/", response_class=HTMLResponse)
def home(request: Request, message: str = "", error: str = ""):
    cfg = settings()
    open_now = submission_open() and cfg.get("submission_enabled", "1") == "1"
    return templates.TemplateResponse("index.html", {
        "request": request, "cfg": cfg, "open_now": open_now,
        "message": message, "error": error, "week": week_key()
    })

@app.post("/submit")
def submit(
    request: Request,
    telegram_nick: str = Form(...),
    tiktok_nick: str = Form(...),
    url: str = Form(...)
):
    cfg = settings()
    if not submission_open() or cfg.get("submission_enabled", "1") != "1":
        return RedirectResponse("/?error=Приём%20ссылок%20на%20эту%20неделю%20закрыт", status_code=303)
    telegram_nick = telegram_nick.strip()
    tiktok_nick = tiktok_nick.strip()
    url = url.strip()
    if not telegram_nick or not tiktok_nick or len(telegram_nick) > 80 or len(tiktok_nick) > 80:
        return RedirectResponse("/?error=Проверьте%20ники", status_code=303)
    if not valid_tiktok_url(url):
        return RedirectResponse("/?error=Вставьте%20корректную%20ссылку%20на%20видео%20TikTok", status_code=303)
    if not SOURCEVINE_API_KEY:
        return RedirectResponse("/?error=Автоматическая%20проверка%20пока%20не%20настроена", status_code=303)

    canonical = canonicalize(url)
    # Получаем статистику ровно один раз при отправке формы. Повторного опроса нет.
    stats = None
    stats_error = ""
    try:
        stats = fetch_tiktok_stats(url)
    except Exception as exc:
        # Не подменяем ошибку нулевыми просмотрами; сохраняем отправку с пометкой ошибки.
        stats_error = str(exc)[:500]

    try:
        with db() as c:
            if stats is not None:
                c.execute("""INSERT INTO submissions
                    (telegram_nick,tiktok_nick,url,canonical_url,week,submitted_at,
                     views,likes,comments,shares,stats_status,notes)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (telegram_nick, tiktok_nick, url, canonical, week_key(), now_local().isoformat(),
                     stats["views"], stats["likes"], stats["comments"], stats["shares"], "checked", "Sourcevine: однократная проверка"))
            else:
                c.execute("""INSERT INTO submissions
                    (telegram_nick,tiktok_nick,url,canonical_url,week,submitted_at,stats_status,notes)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (telegram_nick, tiktok_nick, url, canonical, week_key(), now_local().isoformat(), "error", "Sourcevine: " + stats_error))
    except sqlite3.IntegrityError:
        return RedirectResponse("/?error=Такая%20ссылка%20уже%20была%20отправлена", status_code=303)

    if stats is not None:
        return RedirectResponse("/?message=Ссылка%20принята.%20Статистика%20получена%20и%20сохранена", status_code=303)
    return RedirectResponse("/?error=Ссылка%20сохранена%2C%20но%20Sourcevine%20не%20вернул%20статистику.%20Проверьте%20результат%20в%20админ-панели", status_code=303)

@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, all: int = 0, week: str = "", page: int = 1):
    if not request.session.get("is_admin"):
        return RedirectResponse("/admin/login", status_code=303)
    cfg = settings()
    selected_week = week or week_key()
    page = max(1, page)
    per_page = 50
    with db() as c:
        rows = c.execute("SELECT * FROM submissions WHERE week=? ORDER BY submitted_at DESC", (selected_week,)).fetchall()
        visible = []
        for r in rows:
            status = calculate_status(r, cfg)
            # Default view only shows eligible videos; AI flags are surfaced if set.
            if all or status == "qualified" or r["ai_flag"] in ("review", "high_risk"):
                d = dict(r)
                d["result_status"] = status
                d["payout"] = round((d["views"] or 0) / 1000 * float(cfg.get("usd_per_1000", "2")), 2) if status == "qualified" else 0
                visible.append(d)
        total = len(visible)
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        visible = visible[(page-1)*per_page:page*per_page]
        weeks = [x["week"] for x in c.execute("SELECT DISTINCT week FROM submissions ORDER BY week DESC").fetchall()]
        aggregate = c.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(views),0) AS views,
                   COALESCE(SUM(likes),0) AS likes, COALESCE(SUM(comments),0) AS comments,
                   COALESCE(SUM(shares),0) AS shares
            FROM submissions WHERE week=? AND views IS NOT NULL
        """, (selected_week,)).fetchone()
    return templates.TemplateResponse("admin.html", {
        "request": request, "cfg": cfg, "rows": visible, "all": all,
        "week": selected_week, "weeks": weeks, "aggregate": dict(aggregate),
        "page": page, "pages": pages, "total": total
    })

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error, "cfg": settings()})

@app.post("/admin/login")
def admin_login_post(request: Request, password: str = Form(...)):
    if not ADMIN_PASSWORD:
        return RedirectResponse("/admin/login?error=Пароль%20администратора%20не%20настроен%20на%20сервере", status_code=303)
    if not hmac.compare_digest(password, ADMIN_PASSWORD):
        return RedirectResponse("/admin/login?error=Неверный%20пароль", status_code=303)
    request.session["is_admin"] = True
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)

@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings(request: Request, saved: str = ""):
    if not request.session.get("is_admin"):
        return RedirectResponse("/admin/login", status_code=303)
    return templates.TemplateResponse("settings.html", {"request": request, "cfg": settings(), "saved": saved})

@app.post("/admin/settings")
def admin_settings_post(
    request: Request,
    site_name: str = Form(...),
    min_views: int = Form(...),
    max_views: int = Form(...),
    usd_per_1000: float = Form(...),
    public_domain: str = Form(""),
    submission_enabled: str = Form("0")
):
    if not request.session.get("is_admin"):
        return RedirectResponse("/admin/login", status_code=303)
    if min_views < 0 or max_views < min_views or usd_per_1000 < 0:
        raise HTTPException(status_code=400, detail="Invalid settings")
    values = {
        "site_name": site_name[:100],
        "min_views": str(min_views),
        "max_views": str(max_views),
        "usd_per_1000": str(usd_per_1000),
        "public_domain": public_domain.strip()[:200],
        "submission_enabled": "1" if submission_enabled == "1" else "0"
    }
    with db() as c:
        for k, v in values.items():
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    return RedirectResponse("/admin/settings?saved=1", status_code=303)

@app.post("/admin/submission/{submission_id}/stats")
def admin_set_stats(request: Request, submission_id: int,
                    views: int = Form(...), likes: int = Form(0),
                    comments: int = Form(0), shares: int = Form(0),
                    ai_flag: str = Form("not_checked"), ai_reason: str = Form("")):
    # Temporary manual test/import endpoint until a real TikTok data provider is configured.
    if not request.session.get("is_admin"):
        return RedirectResponse("/admin/login", status_code=303)
    if min(views, likes, comments, shares) < 0 or ai_flag not in ("not_checked", "normal", "review", "high_risk"):
        raise HTTPException(status_code=400, detail="Invalid statistics")
    with db() as c:
        c.execute("""UPDATE submissions SET views=?,likes=?,comments=?,shares=?,stats_status='manual_test',
                     ai_flag=?,ai_reason=? WHERE id=?""",
                  (views, likes, comments, shares, ai_flag, ai_reason[:500], submission_id))
    return RedirectResponse("/admin?all=1", status_code=303)

@app.get("/health")
def health():
    return {"status": "ok", "time": now_local().isoformat(), "stats_provider": "sourcevine" if SOURCEVINE_API_KEY else "not_configured"}_provider": "not_configured"}
