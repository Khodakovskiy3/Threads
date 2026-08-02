"""
Дашборд hodakov.digital — панель для перегляду аналітики, контент-плану,
постів з метриками та таблиці статистики відео.

Запускається як окремий Railway-сервіс або в потоці main.py.
Читає/пише стан через storage.py (Postgres або json-файли як fallback).
"""

from flask import Flask, request, jsonify, render_template_string, redirect
from openai import OpenAI
import requests
import re
import os
import random
from datetime import datetime
from storage import load_json, save_json, init_db

RANDOM_ASSOCIATION_WORDS = [
    "космос", "кулінарія", "спорт", "музика", "мода", "тварини", "подорожі",
    "погода", "гроші", "історія", "медицина", "кіно", "садівництво", "спогади дитинства",
    "весілля", "автомобілі", "море", "гори", "школа", "сон"
]

# ===== КОНФІГ =====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_USER_ID = os.getenv("THREADS_USER_ID")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")  # опціонально — тільки для авто-статистики YouTube Shorts

# Instagram (Reels) і TikTok підключаються через Zernio (zernio.com) — готовий сервіс-агрегатор
# соцмереж, який уже сам зареєстрований і пройшов перевірки в Meta/TikTok. Замість власного
# App ID/Secret і App Review нам потрібен лише один API-ключ. Безкоштовно для перших 2 акаунтів.
# PUBLIC_BASE_URL — адреса цього дашборду в проді (наприклад https://hodakov-digital.up.railway.app),
# потрібна щоб зібрати правильний redirect_url для Zernio.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
ZERNIO_API_KEY = os.getenv("ZERNIO_API_KEY")
ZERNIO_BASE = "https://zernio.com/api/v1"
ZERNIO_STATE_FILE = "zernio_state"

LOG_FILE = "posts_log"
TELEGRAM_LOG_FILE = "telegram_log"
INSIGHTS_FILE = "style_insights"
CONTENT_PLAN_FILE = "content_plan"
VIDEO_STATS_FILE = "video_stats"
NOTIFICATION_SETTINGS_FILE = "notification_settings"

DEFAULT_NOTIFICATION_SETTINGS = {
    "post_stats": True,
    "leads": True,
    "selfpromo": True,
}

app = Flask(__name__)
init_db()


def check_token():
    if not DASHBOARD_TOKEN:
        return True
    return (request.args.get("token") == DASHBOARD_TOKEN
            or request.headers.get("X-Dashboard-Token") == DASHBOARD_TOKEN)


@app.before_request
def guard():
    # Головна сторінка завжди відкривається (токен не потрібен для HTML)
    if request.path == "/" or not request.path.startswith("/api"):
        return None
    # API-маршрути — перевіряємо токен
    if not check_token():
        return jsonify({"error": "unauthorized"}), 401


# ===== API: пости =====
@app.route("/api/posts")
def api_posts():
    threads_posts = load_json(LOG_FILE, [])
    telegram_posts = load_json(TELEGRAM_LOG_FILE, [])
    return jsonify({"threads": threads_posts, "telegram": telegram_posts})


# ===== API: контент-план =====
@app.route("/api/plan")
def api_plan():
    return jsonify(load_json(CONTENT_PLAN_FILE, []))


@app.route("/api/plan/<idea_id>/toggle", methods=["POST"])
def api_plan_toggle(idea_id):
    plan = load_json(CONTENT_PLAN_FILE, [])
    for idea in plan:
        if idea["id"] == idea_id:
            idea["status"] = "done" if idea["status"] != "done" else "todo"
            idea["done_at"] = (datetime.now().strftime("%d.%m.%Y %H:%M")
                               if idea["status"] == "done" else None)
    save_json(CONTENT_PLAN_FILE, plan)
    return jsonify({"ok": True})


def engagement_score(metrics):
    if not metrics:
        return 0
    return (
        metrics.get("views", 0)
        + metrics.get("likes", 0)
        + metrics.get("replies", 0) * 2
        + metrics.get("reposts", 0) * 3
        + metrics.get("quotes", 0) * 3
    )


@app.route("/api/plan/generate", methods=["POST"])
def api_plan_generate():
    count = int(request.json.get("count", 10)) if request.is_json else 10
    insights = load_json(INSIGHTS_FILE, {"insights": None})
    threads_posts = load_json(LOG_FILE, [])
    posts_with_metrics = [p for p in threads_posts if p.get("metrics") and p.get("text") and p.get("source") != "manual"]
    top_posts = sorted(posts_with_metrics, key=lambda p: engagement_score(p["metrics"]), reverse=True)
    top_texts = [p["text"] for p in top_posts[:8]]

    context = ""
    if insights.get("insights"):
        context += f"Аналіз того що вже добре заходить:\n{insights['insights']}\n\n"
    if insights.get("boost_topics"):
        context += "Теми які варто розвивати:\n" + "\n".join(f"- {t}" for t in insights["boost_topics"]) + "\n\n"
    if top_texts:
        context += "ТОП пости за переглядами:\n"
        context += "\n---\n".join(top_texts)
    if not context:
        context = ("Даних по метриках ще нема. Орієнтуйся на больову точку аудиторії: власники малого "
                   "бізнесу які бояться що розробник кине проект або тягнутиме місяцями.")

    random_words = random.sample(RANDOM_ASSOCIATION_WORDS, min(6, len(RANDOM_ASSOCIATION_WORDS)))

    prompt = f"""Ти генеруєш контент-план для Threads акаунту розробника сайтів @hodakov.digital.

ТЕХНІКА: з'єднай нішу (сайти для малого бізнесу, клієнти, AI) з одним із випадкових слів нижче.
Шукай неочевидний, метафоричний зв'язок.

Випадкові слова: {', '.join(random_words)}

{context}

ВЕРИФІКАЦІЯ: кожна ідея має резонувати з перевіреним паттерном — той самий тип гумору,
той самий больовий нерв, той самий формат що вже спрацював.

Згенеруй {count} ідей. Кожна — конкретна ситуація чи думка, не загальна тема.
Формат: рівно {count} рядків, без нумерації."""

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )

    lines = [l.strip("-• \t") for l in response.choices[0].message.content.strip().split("\n") if l.strip()]

    plan = load_json(CONTENT_PLAN_FILE, [])
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    for line in lines:
        plan.append({
            "id": f"idea-{len(plan)}-{int(datetime.now().timestamp()*1000)}-{random.randint(1000, 9999)}",
            "text": line,
            "status": "todo",
            "created_at": now,
            "done_at": None
        })
    save_json(CONTENT_PLAN_FILE, plan)
    return jsonify(plan)


# ===== API: аналіз поста за посиланням =====
def resolve_threads_metrics(url):
    """
    Отримує метрики поста Threads за URL.
    Стратегія:
    1. Нормалізуємо URL (прибираємо трейлінг-слеш і query params)
    2. Шукаємо серед постів акаунту той що збігається
    3. Якщо не знайшли в першій сторінці — пробуємо наступну (cursor pagination)
    4. Повертає dict з metrics і media_id або None
    """
    if not THREADS_ACCESS_TOKEN or not THREADS_USER_ID:
        return None
    try:
        url_normalized = url.strip().rstrip("/").split("?")[0]
        media_id = None
        after_cursor = None

        for _page in range(5):  # до 5 сторінок по 100 постів = 500
            list_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads"
            params = {
                "fields": "id,permalink",
                "limit": 100,
                "access_token": THREADS_ACCESS_TOKEN,
            }
            if after_cursor:
                params["after"] = after_cursor

            resp = requests.get(list_url, params=params, timeout=15)
            data = resp.json()

            for item in data.get("data", []):
                item_url = item.get("permalink", "").rstrip("/").split("?")[0]
                if item_url == url_normalized:
                    media_id = item["id"]
                    break

            if media_id:
                break

            cursors = data.get("paging", {}).get("cursors", {})
            after_cursor = cursors.get("after")
            if not after_cursor or not data.get("data"):
                break

        if not media_id:
            return None

        # Отримуємо всі доступні метрики
        insights_url = f"https://graph.threads.net/v1.0/{media_id}/insights"
        insights_params = {
            "metric": "views,likes,replies,reposts,quotes",
            "access_token": THREADS_ACCESS_TOKEN
        }
        insights_resp = requests.get(insights_url, params=insights_params, timeout=10)
        insights_data = insights_resp.json()

        metrics = {}
        for item_data in insights_data.get("data", []):
            val = item_data.get("values", [{}])[0].get("value", 0)
            metrics[item_data["name"]] = val

        # Також отримуємо базові поля поста (текст, тип, час)
        fields_url = f"https://graph.threads.net/v1.0/{media_id}"
        fields_params = {
            "fields": "id,media_type,timestamp,text,permalink,is_quote_post",
            "access_token": THREADS_ACCESS_TOKEN
        }
        fields_resp = requests.get(fields_url, params=fields_params, timeout=10)
        fields_data = fields_resp.json()

        metrics["_meta"] = {
            "media_id": media_id,
            "media_type": fields_data.get("media_type", ""),
            "timestamp": fields_data.get("timestamp", ""),
            "text_preview": (fields_data.get("text") or "")[:400],
            "is_quote_post": fields_data.get("is_quote_post", False),
            "engagement_score": engagement_score(metrics),
        }
        return metrics

    except Exception as e:
        print(f"Помилка resolve_threads_metrics: {e}")
        return None


def extract_youtube_id(url):
    """Дістає ID відео з youtube.com/watch?v=, youtu.be/ або youtube.com/shorts/ посилань."""
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtube\.com/shorts/|youtu\.be/)([\w-]{11})",
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def resolve_youtube_metrics(url):
    """YouTube Data API v3 віддає перегляди/лайки/коментарі для БУДЬ-ЯКОГО публічного відео
    просто за API-ключем, без OAuth — на відміну від TikTok чи Instagram Reels де такого
    простого способу нема. Працює тільки якщо задано YOUTUBE_API_KEY."""
    if not YOUTUBE_API_KEY:
        return None
    video_id = extract_youtube_id(url)
    if not video_id:
        return None
    try:
        resp = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "statistics", "id": video_id, "key": YOUTUBE_API_KEY},
            timeout=15
        )
        data = resp.json()
        items = data.get("items", [])
        if not items:
            return None
        stats = items[0].get("statistics", {})
        return {
            "views": int(stats.get("viewCount", 0)),
            "likes": int(stats.get("likeCount", 0)),
            "comments": int(stats.get("commentCount", 0)),
            "_meta": {"youtube_video_id": video_id}
        }
    except Exception as e:
        print(f"Помилка resolve_youtube_metrics: {e}")
        return None


# ===== Zernio: підключення Instagram / TikTok без власної реєстрації застосунку =====
# Замість того щоб самим реєструвати App у Meta for Developers і TikTok for Developers
# (App Review, App ID/Secret, redirect URI вручну) — використовуємо готовий сервіс-агрегатор
# Zernio, який уже пройшов усі ці перевірки. Власник тисне "Підключити", логіниться прямо на
# сторінці Instagram/TikTok, і Zernio сам зберігає з'єднання — нам лишається тільки один API-ключ.
# Безкоштовно для перших 2 підключених акаунтів (docs.zernio.com/pricing).
def zernio_headers():
    return {"Authorization": f"Bearer {ZERNIO_API_KEY}"}


def get_or_create_zernio_profile():
    """Zernio групує підключені акаунти в 'профіль' (типу бренд/проєкт) — створюємо один
    раз для hodakov.digital і далі перевикористовуємо той самий id з усіх запитів."""
    state = load_json(ZERNIO_STATE_FILE, {})
    if state.get("profile_id"):
        return state["profile_id"]
    try:
        resp = requests.post(
            f"{ZERNIO_BASE}/profiles",
            headers=zernio_headers(),
            json={"name": "hodakov.digital", "description": "Threads AutoPoster"},
            timeout=15,
        )
        profile_id = resp.json().get("profile", {}).get("_id")
        if profile_id:
            state["profile_id"] = profile_id
            save_json(ZERNIO_STATE_FILE, state)
        return profile_id
    except Exception as e:
        print(f"Помилка get_or_create_zernio_profile: {e}")
        return None


def list_zernio_accounts():
    try:
        resp = requests.get(f"{ZERNIO_BASE}/accounts", headers=zernio_headers(), timeout=15)
        return resp.json().get("accounts", [])
    except Exception as e:
        print(f"Помилка list_zernio_accounts: {e}")
        return []


def get_zernio_account_id(platform):
    """platform: 'instagram' або 'tiktok' (Zernio-назва платформи, не наша назва відео-типу)."""
    state = load_json(ZERNIO_STATE_FILE, {})
    cached = state.get("accounts", {})
    if cached.get(platform):
        return cached[platform]
    for acc in list_zernio_accounts():
        if acc.get("platform") == platform:
            cached[platform] = acc["_id"]
            state["accounts"] = cached
            save_json(ZERNIO_STATE_FILE, state)
            return acc["_id"]
    return None


def resolve_zernio_metrics(zernio_platform, url):
    """Шукає пост/відео за посиланням серед постів підключеного (через Zernio) акаунту —
    працює для Instagram Reels і TikTok однаково через /v1/posts/sync-external."""
    if not ZERNIO_API_KEY:
        return None
    account_id = get_zernio_account_id(zernio_platform)
    if not account_id:
        return None
    try:
        resp = requests.post(
            f"{ZERNIO_BASE}/posts/sync-external",
            headers=zernio_headers(),
            json={"accountId": account_id, "url": url},
            timeout=20,
        )
        data = resp.json()
        if not data.get("found"):
            return None
        post = data.get("post", {})
        a = post.get("analytics", {})
        return {
            "views": a.get("views", 0),
            "likes": a.get("likes", 0),
            "comments": a.get("comments", 0),
            "shares": a.get("shares", 0),
            "_meta": {"zernio_platform_post_id": post.get("platformPostId")},
        }
    except Exception as e:
        print(f"Помилка resolve_zernio_metrics ({zernio_platform}): {e}")
        return None


ZERNIO_VIDEO_PLATFORM = {"instagram": "reels", "tiktok": "tiktok"}


def sync_zernio_posts_into_videos(zernio_platform):
    """Підтягує ВСІ пости/відео підключеного (через Zernio) акаунту і додає ті, яких ще нема
    у таблиці відео на сайті — щоб після підключення Reels/TikTok з'являлись самі, без вставки
    посилань по одному вручну. Вже наявні (за zernio_post_id) записи просто оновлює цифрами.
    Викликається одразу після OAuth-підключення і за запитом з кнопки 'Синхронізувати'."""
    if not ZERNIO_API_KEY:
        return 0
    account_id = get_zernio_account_id(zernio_platform)
    if not account_id:
        return 0
    our_platform = ZERNIO_VIDEO_PLATFORM.get(zernio_platform)
    try:
        resp = requests.post(
            f"{ZERNIO_BASE}/posts/sync-external",
            headers=zernio_headers(),
            json={"accountId": account_id},
            timeout=25,
        )
        posts = resp.json().get("posts", [])
    except Exception as e:
        print(f"Помилка sync_zernio_posts_into_videos ({zernio_platform}): {e}")
        return 0

    videos = load_json(VIDEO_STATS_FILE, [])
    by_zernio_id = {v.get("zernio_post_id"): v for v in videos if v.get("zernio_post_id")}
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    added = 0

    for post in posts:
        post_id = post.get("platformPostId")
        if not post_id:
            continue
        a = post.get("analytics", {}) or {}
        if post_id in by_zernio_id:
            v = by_zernio_id[post_id]
            v["views"] = a.get("views", v.get("views"))
            v["likes"] = a.get("likes", v.get("likes"))
            v["comments"] = a.get("comments", v.get("comments"))
            v["shares"] = a.get("shares", v.get("shares"))
            v["last_refreshed"] = now_str
        else:
            videos.append({
                "id": f"video-{int(datetime.now().timestamp()*1000)}-{random.randint(1000, 9999)}",
                "platform": our_platform,
                "url": post.get("platformPostUrl", ""),
                "views": a.get("views", 0),
                "likes": a.get("likes", 0),
                "comments": a.get("comments", 0),
                "shares": a.get("shares", 0),
                "quotes": None,
                "note": (post.get("content") or "")[:200],
                "added_at": now_str,
                "auto_fetched": True,
                "zernio_post_id": post_id,
                "zernio_platform": zernio_platform,
            })
            added += 1

    if added or by_zernio_id:
        save_json(VIDEO_STATS_FILE, videos)
    return added


@app.route("/api/videos/sync", methods=["POST"])
def api_videos_sync():
    """Ручний тригер синхронізації (кнопка 'Синхронізувати' на дашборді) — підтягує всі
    Reels/TikTok з підключених акаунтів одразу, не чекаючи фонового оновлення."""
    added = 0
    for zp in ("instagram", "tiktok"):
        if get_zernio_account_id(zp):
            added += sync_zernio_posts_into_videos(zp)
    return jsonify({"ok": True, "added": added})


@app.route("/api/social_status")
def api_social_status():
    if not ZERNIO_API_KEY:
        return jsonify({"instagram": False, "tiktok": False})
    accounts = list_zernio_accounts()
    connected = {a.get("platform") for a in accounts}
    state = load_json(ZERNIO_STATE_FILE, {})
    cached = state.get("accounts", {})
    for a in accounts:
        if a.get("platform") in ("instagram", "tiktok"):
            cached[a["platform"]] = a["_id"]
    state["accounts"] = cached
    save_json(ZERNIO_STATE_FILE, state)
    return jsonify({"instagram": "instagram" in connected, "tiktok": "tiktok" in connected})


@app.route("/auth/<platform>/start")
def auth_zernio_start(platform):
    if platform not in ("instagram", "tiktok"):
        return "Невідома платформа", 404
    if not ZERNIO_API_KEY:
        return "Zernio не налаштовано — потрібна env-змінна ZERNIO_API_KEY (zernio.com/signup)", 500
    if not PUBLIC_BASE_URL:
        return "Потрібна env-змінна PUBLIC_BASE_URL", 500

    profile_id = get_or_create_zernio_profile()
    if not profile_id:
        return "Не вдалось створити профіль Zernio — перевір ZERNIO_API_KEY", 500

    token = request.args.get("token", "")
    redirect_url = f"{PUBLIC_BASE_URL}/auth/{platform}/callback"
    if token:
        redirect_url += f"?token={token}"

    try:
        resp = requests.get(
            f"{ZERNIO_BASE}/connect/{platform}",
            params={"profileId": profile_id, "redirect_url": redirect_url},
            headers=zernio_headers(),
            timeout=15,
        )
        auth_url = resp.json().get("authUrl")
        if not auth_url:
            return f"Не вдалось отримати посилання для підключення: {resp.text}", 500
        return redirect(auth_url)
    except Exception as e:
        return f"Помилка Zernio connect: {e}", 500


@app.route("/auth/<platform>/callback")
def auth_zernio_callback(platform):
    if platform not in ("instagram", "tiktok"):
        return "Невідома платформа", 404
    token = request.args.get("token", "")
    # Саме підключення вже відбулось на стороні Zernio до цього редіректу. Одразу підтягуємо
    # всі наявні Reels/TikTok цього акаунту, щоб вони з'явились на сайті самі — без вставки
    # посилань по одному вручну.
    try:
        sync_zernio_posts_into_videos(platform)
    except Exception as e:
        print(f"Помилка первинної синхронізації після підключення {platform}: {e}")
    return redirect(f"/?token={token}#videos" if token else "/#videos")


@app.route("/api/analyze_post", methods=["POST"])
def api_analyze_post():
    body = request.json or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400

    metrics = resolve_threads_metrics(url)
    if metrics is None:
        return jsonify({"error": "Пост не знайдено або немає доступу до API"}), 404

    return jsonify(metrics)


# ===== API: відео =====
@app.route("/api/videos")
def api_videos():
    return jsonify(load_json(VIDEO_STATS_FILE, []))


@app.route("/api/videos", methods=["POST"])
def api_videos_add():
    body = request.json or {}
    platform = body.get("platform", "threads")
    url = body.get("url", "")

    entry = {
        "id": f"video-{int(datetime.now().timestamp()*1000)}-{random.randint(1000, 9999)}",
        "platform": platform,
        "url": url,
        "views": body.get("views"),
        "likes": body.get("likes"),
        "comments": body.get("comments"),
        "shares": body.get("shares"),
        "quotes": body.get("quotes"),
        "note": body.get("note", ""),
        "added_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "auto_fetched": False
    }

    if platform == "threads":
        metrics = resolve_threads_metrics(url)
        if metrics:
            entry["views"] = metrics.get("views", entry["views"])
            entry["likes"] = metrics.get("likes", entry["likes"])
            entry["comments"] = metrics.get("replies", entry["comments"])
            entry["shares"] = metrics.get("reposts", entry["shares"])
            entry["quotes"] = metrics.get("quotes", entry["quotes"])
            entry["auto_fetched"] = True
            # зберігаємо media_id щоб періодичне оновлення в main.py могло питати напряму,
            # без повторного пошуку серед усіх постів акаунту щоразу
            media_id = (metrics.get("_meta") or {}).get("media_id")
            if media_id:
                entry["threads_media_id"] = media_id

    elif platform == "youtube":
        metrics = resolve_youtube_metrics(url)
        if metrics:
            entry["views"] = metrics.get("views", entry["views"])
            entry["likes"] = metrics.get("likes", entry["likes"])
            entry["comments"] = metrics.get("comments", entry["comments"])
            entry["auto_fetched"] = True
            video_id = (metrics.get("_meta") or {}).get("youtube_video_id")
            if video_id:
                entry["youtube_video_id"] = video_id

    elif platform == "reels":
        metrics = resolve_zernio_metrics("instagram", url)
        if metrics:
            entry["views"] = metrics.get("views", entry["views"])
            entry["likes"] = metrics.get("likes", entry["likes"])
            entry["comments"] = metrics.get("comments", entry["comments"])
            entry["shares"] = metrics.get("shares", entry["shares"])
            entry["auto_fetched"] = True
            post_id = (metrics.get("_meta") or {}).get("zernio_platform_post_id")
            if post_id:
                entry["zernio_post_id"] = post_id
                entry["zernio_platform"] = "instagram"

    elif platform == "tiktok":
        metrics = resolve_zernio_metrics("tiktok", url)
        if metrics:
            entry["views"] = metrics.get("views", entry["views"])
            entry["likes"] = metrics.get("likes", entry["likes"])
            entry["comments"] = metrics.get("comments", entry["comments"])
            entry["shares"] = metrics.get("shares", entry["shares"])
            entry["auto_fetched"] = True
            post_id = (metrics.get("_meta") or {}).get("zernio_platform_post_id")
            if post_id:
                entry["zernio_post_id"] = post_id
                entry["zernio_platform"] = "tiktok"

    videos = load_json(VIDEO_STATS_FILE, [])
    videos.append(entry)
    save_json(VIDEO_STATS_FILE, videos)
    return jsonify(entry)


@app.route("/api/videos/<video_id>", methods=["DELETE"])
def api_videos_delete(video_id):
    videos = load_json(VIDEO_STATS_FILE, [])
    remaining = [v for v in videos if v.get("id") != video_id]
    if len(remaining) == len(videos):
        return jsonify({"error": "not found"}), 404
    save_json(VIDEO_STATS_FILE, remaining)
    return jsonify({"ok": True})


@app.route("/api/insights")
def api_insights():
    return jsonify(load_json(INSIGHTS_FILE, {"insights": None}))


# ===== API: примусово попросити воркер (main.py) прогнати аналіз негайно =====
# daily_maintenance в main.py йде за таймером кожні 4 години, а таймер скидається при
# кожному redeploy — тому щоб не чекати до 4 годин після кожного оновлення коду,
# сайт може виставити цей прапорець, і воркер підхопить його в межах хвилини.
FORCE_REFRESH_FILE = "force_analysis_refresh"


@app.route("/api/analyze/refresh", methods=["POST"])
def api_force_refresh():
    save_json(FORCE_REFRESH_FILE, {"requested": True, "requested_at": datetime.now().strftime("%d.%m.%Y %H:%M")})
    return jsonify({"ok": True})


# ===== API: налаштування сповіщень бота (керується тільки звідси, не з самого бота) =====
@app.route("/api/notification_settings")
def api_notification_settings_get():
    return jsonify(load_json(NOTIFICATION_SETTINGS_FILE, DEFAULT_NOTIFICATION_SETTINGS))


@app.route("/api/notification_settings", methods=["POST"])
def api_notification_settings_set():
    body = request.json or {}
    current = load_json(NOTIFICATION_SETTINGS_FILE, DEFAULT_NOTIFICATION_SETTINGS)
    for key in DEFAULT_NOTIFICATION_SETTINGS:
        if key in body:
            current[key] = bool(body[key])
    save_json(NOTIFICATION_SETTINGS_FILE, current)
    return jsonify(current)


# ===== HTML =====
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>hodakov.digital</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  /* Задана палітра (темна, лаймовий акцент) */
  --bg: #181818;
  --surface: #242424;
  --surface2: #2D2D2D;
  --surface3: #333333;
  --border: #343434;
  --glass-blur: none;
  --accent: #C7F060;
  --accent2: #A8D84A;
  --accent-glow: rgba(199,240,96,0.25);
  --accent-ink: #1c2408;
  --purple: #8C7BFF;
  --orange: #F89A52;
  --text: #F3F3F3;
  --muted: #A4A4A4;
  --success: #A8D84A;
  --warning: #F89A52;
  --danger: #e5657a;
  --nav-w: 88px;
  --shell: 22px;
}
* { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body { height:100%; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: 'Inter', -apple-system, sans-serif;
  min-height: 100vh;
  width: 100%;
  padding: 20px 28px 28px calc(var(--nav-w) + 32px);
  overflow-x: hidden;
  position: relative;
}

body::before, body::after { content: none; }

/* ── Header ── */
.header {
  position: sticky; top:20px; z-index: 100;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 22px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.35);
  padding: 15px 22px;
  margin-bottom: 18px;
  display: flex; align-items: center; justify-content: space-between;
  width: 100%;
}
.header-left { display:flex; align-items:center; gap:10px; }
.logo-dot {
  width:8px; height:8px; border-radius:50%;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  box-shadow: 0 0 8px var(--accent);
  animation: pulse 2s infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
.header h1 { font-size:15px; font-weight:700; letter-spacing:-0.3px; color:var(--text); }
.header .badge {
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  color:var(--accent-ink); font-size:11px; padding:3px 10px; border-radius:100px; font-weight:700;
  box-shadow: 0 2px 10px var(--accent-glow);
}

/* ── Sections ── */
.section { display:none; padding:0 2px 20px; }
.section.active { display:block; animation: slideUp 0.22s ease; }
@keyframes slideUp { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }

/* ── Sidebar Navigation (full-height dock) ── */
.sidebar {
  position: fixed; left:20px; top:20px; bottom:20px;
  width: var(--nav-w); z-index: 200;
  display:flex; flex-direction:column; align-items:center; gap:10px;
  padding: 22px 0;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 32px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.35);
}
.sidebar::before {
  content:""; width:36px; height:2px; border-radius:2px;
  background: var(--border); margin-bottom:8px;
}
.nav-item {
  width:48px; height:48px; border-radius:50%;
  display:flex; align-items:center; justify-content:center;
  cursor:pointer; color:var(--muted); transition: all 0.2s ease;
  position: relative; user-select:none;
}
.nav-icon { width:20px; height:20px; transition:transform 0.2s; }
.nav-item:hover { color:var(--text); background: var(--surface2); }
.nav-item.active {
  color:var(--accent-ink);
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  box-shadow: 0 4px 18px var(--accent-glow);
}
.nav-item.active .nav-icon { transform: scale(1.08); }
.nav-bar { display:none; }

/* ── Cards ── */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 20px; padding:18px; margin-bottom:14px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.25);
  transition: border-color 0.2s, background 0.2s;
}
.card:active { border-color: var(--accent); }

/* ── Paired cards (side by side on wider screens) ── */
.pair-grid { display:grid; grid-template-columns:1fr; gap:14px; margin-bottom:14px; }
.pair-grid .card { margin-bottom:0; }
@media (min-width:560px) {
  .pair-grid { grid-template-columns:1fr 1fr; }
}

/* ── Stats grid ── */
.stats-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:16px; }
.stat-card {
  background: var(--surface);
  border:1px solid var(--border);
  border-radius:18px; padding:16px 16px 14px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.25);
}
.stat-card.highlight {
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  border-color: transparent; color: var(--accent-ink);
}
.stat-card.highlight .stat-label, .stat-card.highlight .stat-sub { color: rgba(28,36,8,0.65); }
.stat-card.highlight .stat-value { color: var(--accent-ink); -webkit-text-fill-color: var(--accent-ink); background:none; }
.stat-label { font-size:11px; color:var(--muted); margin-bottom:6px; font-weight:500; }
.stat-value { font-size:26px; font-weight:800; letter-spacing:-1px; line-height:1; }
.stat-value.accent { background: linear-gradient(135deg, var(--accent), var(--accent2)); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; }
.stat-sub { font-size:11px; color:var(--muted); margin-top:4px; }

/* ── Chart ── */
.chart-wrap { position:relative; height:160px; margin-top:4px; }
.chart-title { font-size:12px; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:0.5px; margin-bottom:10px; }

/* ── Insight blocks ── */
.insight-group { margin-bottom:12px; }
.insight-label {
  font-size:11px; font-weight:600; color:var(--muted);
  text-transform:uppercase; letter-spacing:0.6px;
  display:flex; align-items:center; gap:6px; margin-bottom:8px;
}
.insight-label .dot { width:6px; height:6px; border-radius:50%; }
.insight-label .dot.green { background:var(--success); }
.insight-label .dot.red { background:var(--danger); }
.insight-label .dot.blue { background:var(--accent); }
.insight-label .dot.orange { background:var(--warning); }
.insight-item {
  font-size:13px; color:var(--text); padding:8px 10px;
  background:var(--surface2); border:1px solid var(--border); border-radius:12px; margin-bottom:6px;
  line-height:1.5;
}

/* ── Post cards ── */
.post-meta { color:var(--muted); font-size:12px; margin-bottom:8px; display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
.post-text { font-size:14px; line-height:1.65; color:var(--text); }
.metrics-row { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
.metric-pill {
  background:var(--surface2); border:1px solid var(--border);
  border-radius:100px; padding:4px 10px; font-size:12px;
  display:flex; align-items:center; gap:5px; color:var(--muted);
}
.metric-pill strong { color:var(--text); font-weight:600; }
.badge-src {
  display:inline-flex; align-items:center;
  padding:2px 8px; border-radius:100px; font-size:11px; font-weight:600;
}
.badge-src.threads { background:rgba(199,240,96,0.14); color:var(--accent); }
.badge-src.telegram { background:rgba(140,123,255,0.16); color:var(--purple); }
.score-badge {
  margin-left:auto; font-size:12px; font-weight:700;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text;
}

/* ── Buttons ── */
.btn {
  display:flex; align-items:center; justify-content:center; gap:8px;
  width:100%; padding:13px; border:none; border-radius:13px;
  font-size:14px; font-weight:600; cursor:pointer;
  transition:opacity 0.2s, transform 0.1s; font-family:inherit;
  margin-bottom:12px;
}
.btn:active { transform:scale(0.97); opacity:0.85; }
.btn-primary {
  background:linear-gradient(135deg, var(--accent), var(--accent2)); color:var(--accent-ink); font-weight:700;
  box-shadow: 0 4px 18px var(--accent-glow);
}
.btn-secondary {
  background:var(--surface2);
  color:var(--text); border:1px solid var(--border);
}
.btn-sm { padding:9px 14px; font-size:13px; border-radius:10px; width:auto; margin-bottom:0; }

/* ── Inputs ── */
input, select, textarea {
  width:100%; background:var(--surface2); color:var(--text);
  border:1px solid var(--border); border-radius:13px;
  padding:11px 14px; margin-bottom:10px; font-size:14px;
  font-family:inherit; outline:none; transition:border-color 0.2s, background 0.2s;
}
input:focus, select:focus, textarea:focus { border-color:var(--accent); background:var(--surface3); }
input::placeholder { color:var(--muted); opacity:0.8; }
select option { background: var(--surface2); color: var(--text); }
.input-row { display:flex; gap:8px; }
.input-row input { flex:1; margin-bottom:0; }

/* ── Plan cards ── */
.idea-card { display:flex; gap:12px; align-items:flex-start; }
.idea-check {
  width:20px; height:20px; border:2px solid var(--border); border-radius:6px;
  flex-shrink:0; margin-top:1px; cursor:pointer;
  display:flex; align-items:center; justify-content:center;
  transition:all 0.2s; background:transparent;
}
.idea-check.checked { background:var(--accent); border-color:var(--accent); }
.idea-check svg { opacity:0; transition:opacity 0.15s; }
.idea-check.checked svg { opacity:1; }
.idea-card.done .post-text { opacity:0.35; text-decoration:line-through; }
.idea-date { font-size:11px; color:var(--muted); margin-top:4px; }

/* ── Analyze post ── */
.analyze-result {
  background:var(--surface2); border:1px solid var(--border);
  border-radius:16px; padding:14px; margin-top:12px;
  backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
}
.analyze-meta { font-size:12px; color:var(--muted); margin-bottom:10px; }
.analyze-score {
  font-size:32px; font-weight:800; letter-spacing:-1px;
  background:linear-gradient(135deg, var(--accent), var(--accent2));
  -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text;
}
.analyze-text { font-size:13px; color:var(--text); margin-top:8px; line-height:1.55; opacity:0.8; }

/* ── Videos ── */
.video-card-url {
  font-size:12px; color:var(--accent); word-break:break-all; margin-bottom:6px; text-decoration:none;
}
.tag { display:inline-block; padding:2px 8px; border-radius:100px; font-size:11px; font-weight:600;
       background:var(--surface3); border:1px solid var(--border); color:var(--muted); margin-right:4px; }

/* ── Empty / Loader ── */
.empty { text-align:center; padding:44px 20px; color:var(--muted); }
.empty-icon { font-size:36px; margin-bottom:14px; opacity:0.4; }
.loader { display:flex; align-items:center; justify-content:center; padding:36px; color:var(--muted); font-size:14px; gap:8px; }
.spinner { width:16px; height:16px; border:2px solid var(--border); border-top-color:var(--accent); border-radius:50%; animation:spin 0.7s linear infinite; }
@keyframes spin { to{transform:rotate(360deg)} }
.section-title { font-size:16px; font-weight:700; margin-bottom:14px; }

/* ── Toggle switch (сповіщення) ── */
.toggle-row {
  display:flex; align-items:center; justify-content:space-between;
  padding:12px 2px; border-bottom:1px solid var(--border);
}
.toggle-row:last-child { border-bottom:none; }
.toggle-label { font-size:14px; color:var(--text); font-weight:500; }
.toggle-sub { font-size:12px; color:var(--muted); margin-top:2px; }
.switch { position:relative; width:44px; height:26px; flex-shrink:0; }
.switch input { opacity:0; width:0; height:0; }
.switch-track {
  position:absolute; inset:0; background:var(--surface3); border:1px solid var(--border);
  border-radius:100px; cursor:pointer; transition:background 0.2s;
  backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
}
.switch-track:before {
  content:""; position:absolute; width:20px; height:20px; left:2px; top:2px;
  background:var(--muted); border-radius:50%; transition:transform 0.2s, background 0.2s;
}
.switch input:checked + .switch-track { background:linear-gradient(135deg, var(--accent), var(--accent2)); border-color:transparent; }
.switch input:checked + .switch-track:before { transform:translateX(18px); background:#fff; }

/* ── Paused badge ── */
.paused-item {
  background:rgba(251,191,36,0.1); border:1px solid rgba(251,191,36,0.25);
  border-radius:12px; padding:7px 10px; font-size:12px; color:var(--warning); margin-bottom:6px;
  backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
}

/* ── Improvements ── */
.improvement-card {
  background:var(--surface2); border:1px solid var(--border);
  border-left:3px solid var(--accent2);
  border-radius:0 14px 14px 0;
  padding:12px; margin-bottom:8px; font-size:13px; line-height:1.55;
}

/* ── Desktop: full-screen, no centered container ── */
@media (min-width:640px) {
  .stats-grid { grid-template-columns:repeat(4,1fr); }
  .chart-wrap { height:220px; }
}
@media (max-width:420px) {
  :root { --nav-w:64px; }
  .sidebar { left:8px; top:8px; bottom:8px; padding:14px 0; border-radius:24px; }
  .nav-item { width:40px; height:40px; }
  body { padding:12px 12px 20px calc(var(--nav-w) + 16px); }
}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="logo-dot"></div>
    <h1>hodakov.digital</h1>
  </div>
  <span class="badge">панель</span>
</div>

<!-- Sections -->
<div id="analytics" class="section active">
  <div class="stats-grid" id="stats-grid">
    <div class="stat-card highlight"><div class="stat-label">Всього постів</div><div class="stat-value" style="color:var(--border)">—</div></div>
    <div class="stat-card"><div class="stat-label">Сер. перегляди</div><div class="stat-value" style="color:var(--border)">—</div></div>
    <div class="stat-card"><div class="stat-label">Всього переглядів</div><div class="stat-value" style="color:var(--border)">—</div></div>
    <div class="stat-card"><div class="stat-label">Топ скор</div><div class="stat-value" style="color:var(--border)">—</div></div>
  </div>
  <button class="btn btn-secondary" style="margin-bottom:12px" onclick="forceRefreshAnalysis(this)">
    Оновити аналіз зараз (не чекати до 4 год)
  </button>
  <div id="insights-zone"><div class="loader"><div class="spinner"></div>Завантаження...</div></div>
  <div class="card">
    <div class="insight-label"><div class="dot blue"></div>Аналіз поста за посиланням</div>
    <div class="input-row">
      <input id="analyze-url" placeholder="https://www.threads.net/@.../post/..." type="url">
      <button class="btn btn-primary btn-sm" onclick="analyzePost()">Аналіз</button>
    </div>
    <div id="analyze-result"></div>
  </div>
</div>
<div id="posts" class="section"></div>
<div id="plan" class="section"></div>
<div id="videos" class="section"></div>
<div id="notifications" class="section"></div>

<!-- Sidebar Navigation -->
<nav class="sidebar">
  <div class="nav-item active" data-tab="analytics" title="Аналіз">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M18 20V10"/><path d="M12 20V4"/><path d="M6 20v-6"/>
    </svg>
  </div>
  <div class="nav-item" data-tab="posts" title="Пости">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
    </svg>
  </div>
  <div class="nav-item" data-tab="plan" title="Контент-план">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/>
      <line x1="8" y1="18" x2="21" y2="18"/>
      <line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>
    </svg>
  </div>
  <div class="nav-item" data-tab="videos" title="Відео">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
    </svg>
  </div>
  <div class="nav-item" data-tab="notifications" title="Сповіщення">
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>
    </svg>
  </div>
</nav>

<script>
const tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) tg.expand();

const qs = new URLSearchParams(location.search);
const token = qs.get('token') || '';
function withToken(url) {
  return url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token);
}

// ── Navigation ──
document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', () => {
    const tab = item.dataset.tab;
    document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    item.classList.add('active');
    document.getElementById(tab).classList.add('active');
    sectionLoaders[tab] && sectionLoaders[tab]();
  });
});

// ── Charts registry ──
const charts = {};
function destroyChart(id) { if (charts[id]) { charts[id].destroy(); delete charts[id]; } }

function chartDefaults() {
  return {
    responsive: true, maintainAspectRatio: false,
    animation: { duration: 500, easing: 'easeOutQuart' },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: '#2D2D2D',
        borderColor: '#343434',
        borderWidth: 1,
        titleColor: '#F3F3F3',
        bodyColor: '#A4A4A4',
        padding: 10,
        cornerRadius: 8,
      }
    },
    scales: {
      x: { grid: { color: '#343434' }, ticks: { color: '#A4A4A4', font: { size: 11 } } },
      y: { grid: { color: '#343434' }, ticks: { color: '#A4A4A4', font: { size: 11 } } }
    }
  };
}

function engScore(m) {
  if (!m) return 0;
  return (m.views||0) + (m.likes||0) + (m.replies||0)*2 + (m.reposts||0)*3 + (m.quotes||0)*3;
}

// ── Helpers ──
function metricPills(m, extra) {
  if (!m) return '';
  const items = [
    ['👁', m.views, 'перегляди'],
    ['♥', m.likes, 'лайки'],
    ['↩', m.replies, 'відповіді'],
    ['↺', m.reposts, 'репости'],
    ['❝', m.quotes, 'цитати'],
  ].filter(([,v]) => v !== undefined && v !== null);
  if (!items.length) return '';
  return `<div class="metrics-row">${items.map(([icon, v, title]) =>
    `<div class="metric-pill" title="${title}">${icon} <strong>${v ?? '–'}</strong></div>`
  ).join('')}${extra || ''}</div>`;
}

function loader() { return `<div class="loader"><div class="spinner"></div>Завантаження...</div>`; }
function empty(icon, text) { return `<div class="empty"><div class="empty-icon">${icon}</div>${text}</div>`; }

// ═══════════════════════════════════════════════════
// ── ANALYTICS SECTION ──
// ═══════════════════════════════════════════════════
let analyticsLoaded = false;
async function loadAnalytics() {
  if (analyticsLoaded) return;
  analyticsLoaded = true;
  // Скелет вже є в HTML — просто завантажуємо дані
  try {
    const [postsRes, insRes] = await Promise.all([
      fetch(withToken('/api/posts')),
      fetch(withToken('/api/insights'))
    ]);

    if (!postsRes.ok || !insRes.ok) {
      const code = !postsRes.ok ? postsRes.status : insRes.status;
      document.getElementById('insights-zone').innerHTML =
        `<div class="card" style="border-color:var(--danger)">
           <div style="color:var(--danger);font-weight:600;margin-bottom:6px">Помилка API (${code})</div>
           <div style="font-size:13px;color:var(--muted)">
             ${code === 401 ? 'Не авторизовано — додайте ?token=... до URL' : 'Сервер повернув помилку ' + code}
           </div>
         </div>`;
      return;
    }

    const postsData = await postsRes.json();
    const ins = await insRes.json();

    const threads = Array.isArray(postsData.threads) ? postsData.threads : [];
    const telegram = Array.isArray(postsData.telegram) ? postsData.telegram : [];
    const all = [
      ...threads.map(p => ({...p, source:'threads'})),
      ...telegram.map(p => ({...p, source:'telegram'}))
    ].sort((a,b) => (b.timestamp||'').localeCompare(a.timestamp||''));

    const withMetrics = all.filter(p => p.metrics && p.source === 'threads');
    const totalViews = withMetrics.reduce((s,p) => s + (p.metrics.views||0), 0);
    const avgViews = withMetrics.length ? Math.round(totalViews / withMetrics.length) : 0;
    const sorted = [...withMetrics].sort((a,b) => engScore(b.metrics) - engScore(a.metrics));
    const topScore = sorted.length ? engScore(sorted[0].metrics) : 0;

    // Оновлюємо stats grid
    document.getElementById('stats-grid').innerHTML = `
      <div class="stat-card highlight">
        <div class="stat-label">Всього постів</div>
        <div class="stat-value">${all.length}</div>
        <div class="stat-sub">${threads.length} Threads · ${telegram.length} TG</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Сер. перегляди</div>
        <div class="stat-value accent">${avgViews.toLocaleString()}</div>
        <div class="stat-sub">за пост</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Всього переглядів</div>
        <div class="stat-value">${totalViews.toLocaleString()}</div>
        <div class="stat-sub">в Threads</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Топ скор</div>
        <div class="stat-value accent">${topScore}</div>
        <div class="stat-sub">найкращий пост</div>
      </div>`;

    const chartPosts = [...withMetrics]
      .sort((a,b) => (a.timestamp||'').localeCompare(b.timestamp||''))
      .slice(-20);

    let iz = '';

    if (chartPosts.length > 1) {
      iz += `<div class="card">
        <div class="chart-title">Перегляди — останні ${chartPosts.length} постів</div>
        <div class="chart-wrap"><canvas id="viewsChart"></canvas></div>
      </div>`;
    }

    if (ins.insights) {
      iz += `<div class="card">
        <div class="section-title" style="font-size:14px;margin-bottom:10px">Аналіз постів</div>
        <div style="font-size:13px;line-height:1.65;color:#D0D0D0;margin-bottom:10px">${ins.insights}</div>
        <div class="stat-sub">Оновлено: ${ins.updated_at||'—'} · на основі ${ins.based_on_posts||'?'} постів</div>
      </div>`;
    }

    if ((ins.winning_patterns && ins.winning_patterns.length) || (ins.avoid_patterns && ins.avoid_patterns.length)) {
      iz += `<div class="pair-grid">`;
      if (ins.winning_patterns && ins.winning_patterns.length) {
        iz += `<div class="card">
          <div class="insight-label"><div class="dot green"></div>Що працює</div>
          ${ins.winning_patterns.map(w => `<div class="insight-item">✓ ${w}</div>`).join('')}
        </div>`;
      }
      if (ins.avoid_patterns && ins.avoid_patterns.length) {
        iz += `<div class="card">
          <div class="insight-label"><div class="dot red"></div>Чого уникати</div>
          ${ins.avoid_patterns.map(a => `<div class="insight-item">✗ ${a}</div>`).join('')}
        </div>`;
      }
      iz += `</div>`;
    }

    if (ins.boost_topics && ins.boost_topics.length) {
      iz += `<div class="card">
        <div class="insight-label"><div class="dot blue"></div>Розвивати далі</div>
        ${ins.boost_topics.map(t => `<div class="insight-item">→ ${t}</div>`).join('')}
      </div>`;
    }

    const paused = [];
    (ins.paused_topics||[]).forEach(t => paused.push(`Тема "${t.topic_id}" — пауза до ${t.until}`));
    (ins.paused_angles||[]).forEach(a => paused.push(`Кут "${a.angle_id}" — пауза до ${a.until}`));
    if (paused.length) {
      iz += `<div class="card">
        <div class="insight-label"><div class="dot orange"></div>На паузі</div>
        ${paused.map(p => `<div class="paused-item">⏸ ${p}</div>`).join('')}
      </div>`;
    }

    if (ins.post_improvements) {
      iz += `<div class="card">
        <div class="insight-label"><div class="dot blue"></div>Покращення слабких постів</div>
        ${ins.post_improvements.split('\\n').filter(l=>l.trim()).map(l =>
          `<div class="improvement-card">${l}</div>`).join('')}
      </div>`;
    }

    if (!ins.insights && !withMetrics.length) {
      iz += `<div class="empty"><div class="empty-icon">📊</div>
        Аналіз з'явиться після 12+ постів з метриками (старших 24 год)</div>`;
    }

    document.getElementById('insights-zone').innerHTML = iz;

    if (chartPosts.length > 1) {
      const ctx = document.getElementById('viewsChart').getContext('2d');
      destroyChart('views');
      charts['views'] = new Chart(ctx, {
        type: 'line',
        data: {
          labels: chartPosts.map(p => (p.timestamp||'').slice(0,5)),
          datasets: [{
            data: chartPosts.map(p => p.metrics.views || 0),
            borderColor: '#C7F060',
            backgroundColor: 'rgba(199,240,96,0.15)',
            borderWidth: 2.5,
            pointBackgroundColor: '#C7F060',
            pointRadius: 4,
            tension: 0.4,
            fill: true,
          }]
        },
        options: chartDefaults()
      });
    }

  } catch(err) {
    document.getElementById('insights-zone').innerHTML =
      `<div class="card" style="border-color:var(--danger)">
         <div style="color:var(--danger);font-weight:600;margin-bottom:6px">Помилка завантаження</div>
         <div style="font-size:13px;color:var(--muted)">${err.message}</div>
         <button class="btn btn-secondary" style="margin-top:10px;font-size:13px"
           onclick="analyticsLoaded=false;loadAnalytics()">Спробувати знову</button>
       </div>`;
  }
}

async function forceRefreshAnalysis(btn) {
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Прошу воркер оновити...';
  try {
    await fetch(withToken('/api/analyze/refresh'), {method: 'POST'});
    btn.textContent = 'Готово, оновиться протягом хвилини';
    setTimeout(() => { btn.disabled = false; btn.textContent = original; }, 8000);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = original;
    alert('Не вдалось: ' + err.message);
  }
}

async function analyzePost() {
  const url = document.getElementById('analyze-url').value.trim();
  if (!url) return;
  const res = document.getElementById('analyze-result');
  res.innerHTML = loader();

  try {
    const resp = await fetch(withToken('/api/analyze_post'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url})
    });
    const data = await resp.json();
    if (data.error) {
      res.innerHTML = `<div style="color:var(--danger);font-size:13px;padding:8px 0">${data.error}</div>`;
      return;
    }
    const meta = data._meta || {};
    const score = meta.engagement_score || 0;
    res.innerHTML = `<div class="analyze-result">
      <div class="analyze-meta">${meta.media_type || 'TEXT'} · ${meta.timestamp ? new Date(meta.timestamp).toLocaleDateString('uk') : ''}</div>
      <div class="analyze-score">${score} балів</div>
      ${metricPills(data)}
      ${meta.text_preview ? `<div class="analyze-text">${meta.text_preview}</div>` : ''}
    </div>`;
  } catch(e) {
    res.innerHTML = `<div style="color:var(--danger);font-size:13px;padding:8px 0">Помилка запиту</div>`;
  }
}

// ═══════════════════════════════════════════════════
// ── POSTS SECTION ──
// ═══════════════════════════════════════════════════
let postsCache = null;

async function loadPosts() {
  const el = document.getElementById('posts');
  if (!postsCache) {
    el.innerHTML = loader();
    try {
      const res = await fetch(withToken('/api/posts'));
      if (!res.ok) { el.innerHTML = empty('⚠️', 'Помилка API ' + res.status); return; }
      const data = await res.json();
      const threads = Array.isArray(data.threads) ? data.threads : [];
      const telegram = Array.isArray(data.telegram) ? data.telegram : [];
      postsCache = [
        ...threads.map(p => ({...p, platform: 'threads'})),
        ...telegram.map(p => ({...p, platform: 'telegram'}))
      ];
    } catch (err) {
      el.innerHTML = empty('⚠️', 'Помилка: ' + err.message);
      return;
    }
  }
  renderPosts();
}

function postsControlsBar(sortBy, filterBy) {
  const sOpt = (v, l) => `<option value="${v}" ${sortBy === v ? 'selected' : ''}>${l}</option>`;
  const fOpt = (v, l) => `<option value="${v}" ${filterBy === v ? 'selected' : ''}>${l}</option>`;
  return `<div class="card" style="margin-bottom:12px;display:flex;gap:8px;flex-wrap:wrap">
    <select id="posts-sort" onchange="renderPosts()" style="margin:0;flex:1;min-width:150px">
      ${sOpt('timestamp', 'Спочатку нові')}
      ${sOpt('score', 'Топ за скором')}
      ${sOpt('views', 'Топ за переглядами')}
      ${sOpt('likes', 'Топ за лайками')}
      ${sOpt('replies', 'Топ за відповідями')}
      ${sOpt('reposts', 'Топ за репостами')}
    </select>
    <select id="posts-filter" onchange="renderPosts()" style="margin:0;flex:1;min-width:150px">
      ${fOpt('all', 'Всі пости')}
      ${fOpt('bot', 'Тільки бот')}
      ${fOpt('manual', 'Тільки особисті')}
    </select>
  </div>`;
}

function renderPosts() {
  const el = document.getElementById('posts');
  const sortEl = document.getElementById('posts-sort');
  const filterEl = document.getElementById('posts-filter');
  const sortBy = sortEl ? sortEl.value : 'timestamp';
  const filterBy = filterEl ? filterEl.value : 'all';

  if (!postsCache || !postsCache.length) {
    el.innerHTML = postsControlsBar(sortBy, filterBy) + empty('📝', 'Постів ще нема');
    return;
  }

  let list = postsCache.slice();
  if (filterBy === 'bot') list = list.filter(p => p.type !== 'manual');
  if (filterBy === 'manual') list = list.filter(p => p.type === 'manual');

  const sorters = {
    timestamp: (a, b) => (b.timestamp || '').localeCompare(a.timestamp || ''),
    score: (a, b) => engScore(b.metrics) - engScore(a.metrics),
    views: (a, b) => (b.metrics?.views || 0) - (a.metrics?.views || 0),
    likes: (a, b) => (b.metrics?.likes || 0) - (a.metrics?.likes || 0),
    replies: (a, b) => (b.metrics?.replies || 0) - (a.metrics?.replies || 0),
    reposts: (a, b) => (b.metrics?.reposts || 0) - (a.metrics?.reposts || 0),
  };
  list.sort(sorters[sortBy] || sorters.timestamp);

  const rows = !list.length
    ? empty('📝', 'Нічого не знайдено для цього фільтра')
    : list.map(p => {
        const score = engScore(p.metrics);
        const isManual = p.type === 'manual';
        return `<div class="card" style="${isManual ? 'opacity:0.65;border-left:3px solid var(--warning)' : ''}">
          <div class="post-meta">
            <span class="badge-src ${p.platform}">${p.platform === 'threads' ? 'Threads' : 'Telegram'}</span>
            ${p.type ? `<span class="tag" style="${isManual ? 'background:rgba(245,158,11,0.15);color:var(--warning)' : ''}">${isManual ? 'особисте' : p.type}</span>` : ''}
            <span>${p.timestamp || ''}</span>
            ${p.status ? `<span style="color:${p.status === 'published' ? 'var(--success)' : 'var(--muted)'}">· ${p.status}</span>` : ''}
            ${score ? `<span class="score-badge">${score} pt</span>` : ''}
          </div>
          <div class="post-text">${(p.text || '').slice(0, 320)}</div>
          ${metricPills(p.metrics)}
        </div>`;
      }).join('');

  el.innerHTML = postsControlsBar(sortBy, filterBy) + rows;
}

// ═══════════════════════════════════════════════════
// ── PLAN SECTION ──
// ═══════════════════════════════════════════════════
async function loadPlan() {
  const el = document.getElementById('plan');
  el.innerHTML = loader();
  try {
  const res = await fetch(withToken('/api/plan'));
  if (!res.ok) { el.innerHTML = empty('⚠️', 'Помилка API ' + res.status); return; }
  const ideas = await res.json();

  el.innerHTML = `
    <button class="btn btn-primary" onclick="generateIdeas(this)">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/>
      </svg>
      Згенерувати нові ідеї
    </button>
    ${ideas.length ? ideas.slice().reverse().map(i => `
      <div class="card idea-card ${i.status==='done' ? 'done' : ''}" id="idea-${i.id}">
        <div class="idea-check ${i.status==='done'?'checked':''}" onclick="togglePlan('${i.id}', this)">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M2 6l3 3 5-5" stroke="#1c2408" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </div>
        <div>
          <div class="post-text">${i.text}</div>
          <div class="idea-date">${i.created_at}</div>
        </div>
      </div>
    `).join('') : empty('💡', 'Ідей ще нема — тисни кнопку вище')}
  `;
  } catch(err) {
    el.innerHTML = empty('⚠️', 'Помилка: ' + err.message);
  }
}

async function togglePlan(id, el) {
  el.classList.toggle('checked');
  const card = document.getElementById('idea-' + id);
  card && card.classList.toggle('done');
  await fetch(withToken(`/api/plan/${id}/toggle`), {method: 'POST'});
}

async function generateIdeas(btn) {
  btn.disabled = true;
  btn.textContent = 'Генерую...';
  try {
    await fetch(withToken('/api/plan/generate'), {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({count:10})
    });
    await loadPlan();
  } finally {
    btn.disabled = false;
    btn.textContent = '+ Згенерувати нові ідеї';
  }
}

// ═══════════════════════════════════════════════════
// ── VIDEOS SECTION ──
// ═══════════════════════════════════════════════════
let videosLoaded = false;
let socialStatus = { instagram: false, tiktok: false };

function connectRow(key, label, note) {
  const connected = socialStatus[key];
  return `<div class="toggle-row">
    <div>
      <div class="toggle-label">${label}</div>
      <div class="toggle-sub">${connected ? note : 'Не підключено'}</div>
    </div>
    ${connected
      ? '<span class="tag" style="color:var(--success);border-color:var(--success)">✓ підключено</span>'
      : `<a href="${withToken('/auth/' + key + '/start')}" class="btn btn-primary btn-sm" style="text-decoration:none">Підключити</a>`}
  </div>`;
}

async function loadVideos(opts) {
  opts = opts || {};
  if (videosLoaded && !opts.force) { maybeAutoSync(); return; }
  videosLoaded = true;
  const el = document.getElementById('videos');
  el.innerHTML = loader();

  const [res, statusRes] = await Promise.all([
    fetch(withToken('/api/videos')),
    fetch(withToken('/api/social_status'))
  ]);
  const videos = await res.json();
  socialStatus = statusRes.ok ? await statusRes.json() : { instagram: false, tiktok: false };

  el.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="insight-label" style="margin-bottom:12px"><div class="dot blue"></div>Підключені акаунти</div>
      ${connectRow('instagram', 'Instagram', 'Reels підтягуються самі, без вставки посилань')}
      ${connectRow('tiktok', 'TikTok', 'Відео підтягуються самі, без вставки посилань')}
      <div style="display:flex;align-items:center;justify-content:space-between;padding-top:10px">
        <div class="stat-sub" style="padding:0 2px">Один раз авторизуєшся — далі всі Reels/TikTok з'являються тут самі.</div>
        <button class="btn btn-secondary btn-sm" id="sync-videos-btn" onclick="syncConnectedAccounts(this)">Синхронізувати зараз</button>
      </div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="insight-label" style="margin-bottom:12px"><div class="dot blue"></div>Додати відео</div>
      <select id="v-platform" onchange="toggleVideoFields()">
        <option value="threads">Threads</option>
        <option value="tiktok">TikTok</option>
        <option value="reels">Instagram Reels</option>
        <option value="youtube">YouTube Shorts</option>
      </select>
      <input id="v-url" placeholder="Посилання на відео" type="url">
      <div id="v-auto-note" class="stat-sub" style="padding:0 2px 10px">
        Достатньо вставити посилання — перегляди, лайки і коментарі підтягнуться самі.
      </div>
      <div id="v-connect-note" class="stat-sub" style="padding:0 2px 10px;display:none">
        Акаунт ще не підключено — підтягнути дані по одному посиланню без цього неможливо (без скрапінгу). Підключи вище або заповни вручну.
      </div>
      <div id="v-manual-fields" style="display:none">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
          <input id="v-views" placeholder="Перегляди" type="number" style="margin:0">
          <input id="v-likes" placeholder="Лайки" type="number" style="margin:0">
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
          <input id="v-comments" placeholder="Коментарі" type="number" style="margin:0">
          <input id="v-shares" placeholder="Репости" type="number" style="margin:0">
        </div>
      </div>
      <button class="btn btn-primary" onclick="addVideo(this)">Додати відео</button>
    </div>
    ${videos.length ? videos.slice().reverse().map(v => `
      <div class="card" id="video-${v.id}">
        <div class="post-meta">
          <span class="tag">${v.platform}</span>
          <span>${v.added_at}</span>
          ${(v.threads_media_id || v.youtube_video_id || v.zernio_post_id)
            ? '<span style="color:var(--success)">· оновлюється само</span>'
            : (v.auto_fetched ? '<span style="color:var(--success)">· авто (одноразово)</span>' : '<span style="color:var(--muted)">· вручну</span>')}
          ${v.last_refreshed ? `<span style="color:var(--muted)">· ${v.last_refreshed}</span>` : ''}
          <span style="margin-left:auto;cursor:pointer;color:var(--muted)" title="Видалити" onclick="deleteVideo('${v.id}')">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
            </svg>
          </span>
        </div>
        <a class="video-card-url" href="${v.url}" target="_blank">${v.url}</a>
        ${metricPills({views:v.views, likes:v.likes, replies:v.comments, reposts:v.shares, quotes:v.quotes})}
      </div>
    `).join('') : empty('🎬', 'Відео ще не додано')}
  `;
  toggleVideoFields();
}

function toggleVideoFields() {
  const platform = document.getElementById('v-platform').value;
  const autoMap = { threads: true, youtube: true, reels: socialStatus.instagram, tiktok: socialStatus.tiktok };
  const autoFetchable = !!autoMap[platform];
  const needsConnect = !autoFetchable && (platform === 'reels' || platform === 'tiktok');
  document.getElementById('v-auto-note').style.display = autoFetchable ? 'block' : 'none';
  document.getElementById('v-connect-note').style.display = needsConnect ? 'block' : 'none';
  document.getElementById('v-manual-fields').style.display = autoFetchable ? 'none' : 'block';
}

let autoSyncedThisSession = false;

async function maybeAutoSync() {
  // Тихо (без спінера) підтягує нові Reels/TikTok у фоні, коли повертаєшся на вкладку —
  // раз на сесію, щоб не смикати Zernio API щоразу при перемиканні вкладок.
  if (autoSyncedThisSession) return;
  autoSyncedThisSession = true;
  try {
    const r = await fetch(withToken('/api/videos/sync'), { method: 'POST' });
    const data = await r.json();
    if (data.added > 0) { videosLoaded = false; await loadVideos({ force: true }); }
  } catch (e) { console.error('фонова синхронізація не вдалась', e); }
}

async function syncConnectedAccounts(btn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Синхронізую...';
  try {
    const r = await fetch(withToken('/api/videos/sync'), { method: 'POST' });
    const data = await r.json();
    videosLoaded = false;
    await loadVideos({ force: true });
    const freshBtn = document.getElementById('sync-videos-btn');
    if (freshBtn) {
      freshBtn.textContent = data.added > 0 ? `Додано ${data.added} нових` : 'Нових немає';
      setTimeout(() => { const b = document.getElementById('sync-videos-btn'); if (b) b.textContent = original; }, 3000);
    }
  } catch (e) {
    btn.disabled = false;
    btn.textContent = original;
    alert('Не вдалось синхронізувати: ' + e.message);
  }
}

async function deleteVideo(id) {
  if (!confirm('Видалити цей запис?')) return;
  try {
    await fetch(withToken(`/api/videos/${id}`), {method: 'DELETE'});
    const card = document.getElementById('video-' + id);
    if (card) card.remove();
  } catch (err) {
    alert('Не вдалось видалити: ' + err.message);
  }
}

async function addVideo(btn) {
  btn.disabled = true; btn.textContent = 'Додаю...';
  const body = {
    platform: document.getElementById('v-platform').value,
    url: document.getElementById('v-url').value,
    views: document.getElementById('v-views').value || null,
    likes: document.getElementById('v-likes').value || null,
    comments: document.getElementById('v-comments').value || null,
    shares: document.getElementById('v-shares').value || null,
  };
  try {
    await fetch(withToken('/api/videos'), {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
    });
    videosLoaded = false;
    await loadVideos();
  } finally {
    btn.disabled = false; btn.textContent = 'Додати відео';
  }
}

// ═══════════════════════════════════════════════════
// ── NOTIFICATIONS SECTION ──
// ═══════════════════════════════════════════════════
const NOTIF_LABELS = {
  post_stats: ['Статистика постів', 'Повідомлення в бота коли для допису підтягнулись перегляди/лайки'],
  leads: ['Потенційні клієнти', 'Сповіщення коли хтось на Threads пише що потрібен розробник сайту'],
  selfpromo: ['Саморекламні тредси', 'Сповіщення про тредси конкурентів з готовою відповіддю на підтвердження'],
};

async function loadNotifications() {
  const el = document.getElementById('notifications');
  el.innerHTML = loader();
  try {
    const res = await fetch(withToken('/api/notification_settings'));
    if (!res.ok) { el.innerHTML = empty('⚠️', 'Помилка API ' + res.status); return; }
    const settings = await res.json();

    el.innerHTML = `<div class="card">
      ${Object.entries(NOTIF_LABELS).map(([key, [label, sub]]) => `
        <div class="toggle-row">
          <div>
            <div class="toggle-label">${label}</div>
            <div class="toggle-sub">${sub}</div>
          </div>
          <label class="switch">
            <input type="checkbox" ${settings[key] ? 'checked' : ''} onchange="toggleNotification('${key}', this.checked)">
            <span class="switch-track"></span>
          </label>
        </div>
      `).join('')}
    </div>
    <div class="stat-sub" style="padding:0 4px">Це керує тим, які повідомлення шле бот в Telegram. Сам бот більше нічого не налаштовує — все тут.</div>`;
  } catch (err) {
    el.innerHTML = empty('⚠️', 'Помилка: ' + err.message);
  }
}

async function toggleNotification(key, value) {
  try {
    await fetch(withToken('/api/notification_settings'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({[key]: value})
    });
  } catch (err) {
    console.error('Не вдалось зберегти налаштування', err);
  }
}

// ── Section loader map ──
const sectionLoaders = {
  analytics: loadAnalytics,
  posts: loadPosts,
  plan: loadPlan,
  videos: loadVideos,
  notifications: loadNotifications,
};

// Initial load
loadAnalytics();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
