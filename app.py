import os
import hmac
import sqlite3
import json
from urllib.request import Request as URLRequest, urlopen
from urllib.parse import urlencode, urlparse
from datetime import datetime, timedelta, timezone
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
    print("WARNING: ADMIN_PASSWORD is not configured.")
if len(SESSION_SECRET) < 32:
    print("WARNING: SESSION_SECRET should be a random value of at least 32 characters.")

app = FastAPI(title="TikTok Weekly Tracker")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET or "unsafe-dev-only-change-me",
    https_only=True,
    same_site="lax",
)
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")


def now_local():
    return datetime.now(TZ)


def week_start(dt=None):
    dt = (dt or now_local()).astimezone(TZ)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=midnight.weekday())


def week_key(dt=None):
    return week_start(dt).date().isoformat()


def submission_open(dt=None):
    dt = (dt or now_local()).astimezone(TZ)
    start = week_start(dt)
    return start <= dt < start + timedelta(days=7)


def valid_tiktok_url(url):
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in ("http", "https")
            and (host == "tiktok.com" or host.endswith(".tiktok.com"))
            and ("/video/" in parsed.path or host in ("vm.tiktok.com", "vt.tiktok.com"))
        )
    except Exception:
        return False


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
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
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        defaults = {
            "site_name": "SIGMA • TikTok Tracker",
            "min_views": "2000",
            "max_views": "1000000",
            "usd_per_1000": "2",
            "public_domain": "",
            "week_timezone": "Europe/Moscow",
            "submission_enabled": "1",
        }
        for key, value in defaults.items():
            c.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                (key, value),
            )


init_db()


def settings():
    with db() as c:
        rows = c.execute("SELECT key,value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


def admin_required(request: Request):
    if not request.session.get("is_admin"):
        raise HTTPException(status_code=401, detail="Admin login required")


def canonicalize(url):
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/")
    return f"https://{(parsed.hostname or '').lower()}{path}"


def fetch_tiktok_stats(url: str) -> dict:
    """Fetch exactly one snapshot of TikTok stats from Sourcevine."""
    if not SOURCEVINE_API_KEY:
        raise ValueError("SOURCEVINE_API_KEY не настроен в Render Environment")

    query = urlencode({"url": url})
    req = URLRequest(
        f"{SOURCEVINE_API_URL}?{query}",
        headers={
            "Authorization": f"Bearer {SOURCEVINE_API_KEY}",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(req, timeout=25) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
            http_status = response.status
    except Exception as exc:
        # HTTPError includes the HTTP status; never include the API key in messages.
        status = getattr(exc, "code", None)
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        if status:
            raise ValueError(f"Sourcevine HTTP {status}: {detail or str(exc)}") from exc
        raise ValueError(f"Sourcevine request failed ({type(exc).__name__}): {exc}") from exc

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Sourcevine вернул не JSON (HTTP {http_status})") from exc

    if not isinstance(payload, dict):
        raise ValueError("Sourcevine вернул ответ неожиданного формата")

    if payload.get("success") is False:
        message = payload.get("error") or payload.get("message") or "неизвестная ошибка"
        raise ValueError(f"Sourcevine success=false: {str(message)[:250]}")

    availability = payload.get("availability")
    if isinstance(availability, dict):
        status = availability.get("status")
        if status and str(status).lower() not in ("available", "ok", "success"):
            raise ValueError(f"Видео недоступно в Sourcevine: {status}")
    elif payload.get("available") is False:
        raise ValueError("Sourcevine сообщает, что видео недоступно")

    # Sourcevine's API response can contain metrics at the top level,
    # under "metrics", or under "data". Support all three shapes.
    data = payload.get("data")
    metrics = payload.get("metrics")
    data = data if isinstance(data, dict) else {}
    metrics = metrics if isinstance(metrics, dict) else {}

    result = {}
    missing = []
    for key in ("views", "likes", "comments", "shares"):
        value = payload.get(key)
        if value is None:
            value = metrics.get(key)
        if value is None:
            value = data.get(key)
        if value is None:
            missing.append(key)
            continue
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Sourcevine вернул некорректное поле {key}") from exc
        if value < 0:
            raise ValueError(f"Sourcevine вернул отрицательное значение {key}")
        result[key] = value

    if missing:
        raise ValueError(
            "Sourcevine не вернул показатели: "
            + ", ".join(missing)
            + f". Верхние поля ответа: {', '.join(map(str, payload.keys()))}"
        )
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
        "request": request,
        "cfg": cfg,
        "open_now": open_now,
        "message": message,
        "error": error,
        "week": week_key(),
    })


@app.post("/submit")
def submit(
    request: Request,
    telegram_nick: str = Form(...),
    tiktok_nick: str = Form(...),
    url: str = Form(...),
):
    cfg = settings()
    if not submission_open() or cfg.get("submission_enabled", "1") != "1":
        return RedirectResponse(
            "/?error=Приём%20ссылок%20на%20эту%20неделю%20закрыт",
            status_code=303,
        )

    telegram_nick = telegram_nick.strip()
    tiktok_nick = tiktok_nick.strip()
    url = url.strip()

    if (
        not telegram_nick
        or not tiktok_nick
        or len(telegram_nick) > 80
        or len(tiktok_nick) > 80
    ):
        return RedirectResponse("/?error=Проверьте%20ники", status_code=303)

    if not valid_tiktok_url(url):
        return RedirectResponse(
            "/?error=Вставьте%20корректную%20ссылку%20на%20видео%20TikTok",
            status_code=303,
        )

    canonical = canonicalize(url)

    # Check duplicates before calling the provider to avoid unnecessary API usage.
    with db() as c:
        existing = c.execute(
            "SELECT 1 FROM submissions WHERE canonical_url=?",
            (canonical,),
        ).fetchone()
    if existing:
        return RedirectResponse(
            "/?error=Такая%20ссылка%20уже%20была%20отправлена",
            status_code=303,
        )

    stats = None
    stats_error = ""
    try:
        stats = fetch_tiktok_stats(url)
    except Exception as exc:
        # Save the failed submission and its diagnostic for the admin panel.
        stats_error = f"{type(exc).__name__}: {exc}"[:500]
        print(f"Sourcevine stats fetch failed: {stats_error}")

    try:
        with db() as c:
            if stats is not None:
                c.execute("""
                    INSERT INTO submissions
                    (telegram_nick,tiktok_nick,url,canonical_url,week,submitted_at,
                     views,likes,comments,shares,stats_status,notes)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    telegram_nick, tiktok_nick, url, canonical, week_key(),
                    now_local().isoformat(), stats["views"], stats["likes"],
                    stats["comments"], stats["shares"], "checked",
                    "Sourcevine: однократная проверка",
                ))
            else:
                c.execute("""
                    INSERT INTO submissions
                    (telegram_nick,tiktok_nick,url,canonical_url,week,submitted_at,
                     stats_status,notes)
                    VALUES(?,?,?,?,?,?,?,?)
                """, (
                    telegram_nick, tiktok_nick, url, canonical, week_key(),
                    now_local().isoformat(), "error", "Sourcevine: " + stats_error,
                ))
    except sqlite3.IntegrityError:
        return RedirectResponse(
            "/?error=Такая%20ссылка%20уже%20была%20отправлена",
            status_code=303,
        )

    if stats is not None:
        return RedirectResponse(
            "/?message=Ссылка%20принята.%20Статистика%20получена%20и%20сохранена",
            status_code=303,
        )
    return RedirectResponse(
        "/?error=Ссылка%20сохранена%2C%20но%20Sourcevine%20не%20вернул%20статистику.%20Администратор%20может%20посмотреть%20ошибку%20в%20логах",
        status_code=303,
    )


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, all: int = 0, week: str = "", page: int = 1):
    if not request.session.get("is_admin"):
        return RedirectResponse("/admin/login", status_code=303)

    cfg = settings()
    selected_week = week or week_key()
    page = max(1, page)
    per_page = 50

    with db() as c:
        rows = c.execute(
            "SELECT * FROM submissions WHERE week=? ORDER BY submitted_at DESC",
            (selected_week,),
        ).fetchall()
        visible = []
        for row in rows:
            status = calculate_status(row, cfg)
            if all or status == "qualified" or row["ai_flag"] in ("review", "high_risk"):
                item = dict(row)
                item["result_status"] = status
                item["payout"] = (
                    round((item["views"] or 0) / 1000 * float(cfg.get("usd_per_1000", "2")), 2)
                    if status == "qualified" else 0
                )
                visible.append(item)

        total = len(visible)
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        visible = visible[(page - 1) * per_page:page * per_page]
        weeks = [
            item["week"]
            for item in c.execute(
                "SELECT DISTINCT week FROM submissions ORDER BY week DESC"
            ).fetchall()
        ]
        aggregate = c.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(views),0) AS views,
                   COALESCE(SUM(likes),0) AS likes, COALESCE(SUM(comments),0) AS comments,
                   COALESCE(SUM(shares),0) AS shares
            FROM submissions WHERE week=? AND views IS NOT NULL
        """, (selected_week,)).fetchone()

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "cfg": cfg,
        "rows": visible,
        "all": all,
        "week": selected_week,
        "weeks": weeks,
        "aggregate": dict(aggregate),
        "page": page,
        "pages": pages,
        "total": total,
    })


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login(request: Request, error: str = ""):
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": error, "cfg": settings()}
    )


@app.post("/admin/login")
def admin_login_post(request: Request, password: str = Form(...)):
    if not ADMIN_PASSWORD:
        return RedirectResponse(
            "/admin/login?error=Пароль%20администратора%20не%20настроен%20на%20сервере",
            status_code=303,
        )
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
    return templates.TemplateResponse(
        "settings.html", {"request": request, "cfg": settings(), "saved": saved}
    )


@app.post("/admin/settings")
def admin_settings_post(
    request: Request,
    site_name: str = Form(...),
    min_views: int = Form(...),
    max_views: int = Form(...),
    usd_per_1000: float = Form(...),
    public_domain: str = Form(""),
    submission_enabled: str = Form("0"),
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
        "submission_enabled": "1" if submission_enabled == "1" else "0",
    }
    with db() as c:
        for key, value in values.items():
            c.execute("""
                INSERT INTO settings(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (key, value))
    return RedirectResponse("/admin/settings?saved=1", status_code=303)


@app.post("/admin/submission/{submission_id}/stats")
def admin_set_stats(
    request: Request,
    submission_id: int,
    views: int = Form(...),
    likes: int = Form(0),
    comments: int = Form(0),
    shares: int = Form(0),
    ai_flag: str = Form("not_checked"),
    ai_reason: str = Form(""),
):
    if not request.session.get("is_admin"):
        return RedirectResponse("/admin/login", status_code=303)
    if min(views, likes, comments, shares) < 0 or ai_flag not in (
        "not_checked", "normal", "review", "high_risk"
    ):
        raise HTTPException(status_code=400, detail="Invalid statistics")

    with db() as c:
        c.execute("""
            UPDATE submissions
            SET views=?,likes=?,comments=?,shares=?,stats_status='manual_test',
                ai_flag=?,ai_reason=?
            WHERE id=?
        """, (views, likes, comments, shares, ai_flag, ai_reason[:500], submission_id))
    return RedirectResponse("/admin?all=1", status_code=303)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "time": now_local().isoformat(),
        "stats_provider": "sourcevine" if SOURCEVINE_API_KEY else "not_configured",
    }
