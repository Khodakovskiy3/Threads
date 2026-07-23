"""
Дашборд hodakov.digital — панель для перегляду аналітики, контент-плану,
постів з метриками та таблиці статистики відео.

Запускається як окремий Railway-сервіс або в потоці main.py.
Читає/пише стан через storage.py (Postgres або json-файли як fallback).
"""

from flask import Flask, request, jsonify, render_template_string
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

LOG_FILE = "posts_log"
TELEGRAM_LOG_FILE = "telegram_log"
INSIGHTS_FILE = "style_insights"
CONTENT_PLAN_FILE = "content_plan"
VIDEO_STATS_FILE = "video_stats"

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
    posts_with_metrics = [p for p in threads_posts if p.get("metrics") and p.get("text")]
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
            "id": f"idea-{len(plan)}-{int(datetime.now().timestamp())}",
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
        "id": f"video-{int(datetime.now().timestamp())}",
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

    videos = load_json(VIDEO_STATS_FILE, [])
    videos.append(entry)
    save_json(VIDEO_STATS_FILE, videos)
    return jsonify(entry)


@app.route("/api/insights")
def api_insights():
    return jsonify(load_json(INSIGHTS_FILE, {"insights": None}))


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
  --bg: #080810;
  --surface: #10101c;
  --surface2: #16162a;
  --surface3: #1e1e35;
  --border: #252540;
  --accent: #6366f1;
  --accent2: #8b5cf6;
  --accent-glow: rgba(99,102,241,0.15);
  --text: #f0f0fa;
  --muted: #6b7090;
  --success: #22c55e;
  --warning: #f59e0b;
  --danger: #ef4444;
  --nav-h: 68px;
}
* { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body { height:100%; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: 'Inter', -apple-system, sans-serif;
  min-height: 100vh;
  padding-bottom: calc(var(--nav-h) + 8px);
  overflow-x: hidden;
}

/* ── Header ── */
.header {
  position: sticky; top: 0; z-index: 100;
  background: rgba(8,8,16,0.88);
  backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--border);
  padding: 12px 16px;
  display: flex; align-items: center; justify-content: space-between;
}
.header-left { display:flex; align-items:center; gap:10px; }
.logo-dot {
  width:8px; height:8px; border-radius:50%;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  box-shadow: 0 0 8px var(--accent);
  animation: pulse 2s infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
.header h1 { font-size:15px; font-weight:700; letter-spacing:-0.3px; }
.header .badge {
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  color:#fff; font-size:11px; padding:3px 10px; border-radius:100px; font-weight:600;
}

/* ── Sections ── */
.section { display:none; padding:14px 14px 20px; }
.section.active { display:block; animation: slideUp 0.22s ease; }
@keyframes slideUp { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }

/* ── Bottom Navigation ── */
.bottom-nav {
  position: fixed; bottom:0; left:0; right:0;
  height: var(--nav-h);
  background: rgba(16,16,28,0.97);
  backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  border-top: 1px solid var(--border);
  display: flex; align-items: stretch; z-index: 200;
  padding-bottom: env(safe-area-inset-bottom, 0);
}
.nav-item {
  flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center;
  gap:3px; cursor:pointer; color:var(--muted); transition:color 0.2s;
  font-size:10px; font-weight:500; letter-spacing:0.2px; user-select:none;
  position: relative;
}
.nav-icon { width:22px; height:22px; transition:transform 0.2s; }
.nav-item.active { color:var(--accent); }
.nav-item.active .nav-icon { transform: scale(1.1); }
.nav-bar {
  position:absolute; top:0; left:50%; transform:translateX(-50%);
  width:28px; height:3px; border-radius:0 0 3px 3px;
  background: linear-gradient(90deg, var(--accent), var(--accent2));
  opacity:0; transition:opacity 0.2s;
}
.nav-item.active .nav-bar { opacity:1; }

/* ── Cards ── */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 16px; padding:16px; margin-bottom:12px;
  transition: border-color 0.2s;
}
.card:active { border-color: var(--accent); }

/* ── Stats grid ── */
.stats-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:14px; }
.stat-card {
  background: var(--surface); border:1px solid var(--border);
  border-radius:14px; padding:14px 14px 12px;
}
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
  background:var(--surface2); border-radius:8px; margin-bottom:6px;
  line-height:1.5;
}

/* ── Post cards ── */
.post-meta { color:var(--muted); font-size:12px; margin-bottom:8px; display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
.post-text { font-size:14px; line-height:1.65; color:var(--text); }
.metrics-row { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
.metric-pill {
  background:var(--surface2); border:1px solid var(--border);
  border-radius:8px; padding:4px 10px; font-size:12px;
  display:flex; align-items:center; gap:5px; color:var(--muted);
}
.metric-pill strong { color:var(--text); font-weight:600; }
.badge-src {
  display:inline-flex; align-items:center;
  padding:2px 8px; border-radius:5px; font-size:11px; font-weight:600;
}
.badge-src.threads { background:rgba(99,102,241,0.12); color:var(--accent); }
.badge-src.telegram { background:rgba(34,197,94,0.12); color:var(--success); }
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
.btn-primary { background:linear-gradient(135deg, var(--accent), var(--accent2)); color:#fff; }
.btn-secondary { background:var(--surface2); color:var(--text); border:1px solid var(--border); }
.btn-sm { padding:9px 14px; font-size:13px; border-radius:10px; width:auto; margin-bottom:0; }

/* ── Inputs ── */
input, select, textarea {
  width:100%; background:var(--surface2); color:var(--text);
  border:1px solid var(--border); border-radius:11px;
  padding:11px 14px; margin-bottom:10px; font-size:14px;
  font-family:inherit; outline:none; transition:border-color 0.2s;
}
input:focus, select:focus, textarea:focus { border-color:var(--accent); }
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
  border-radius:13px; padding:14px; margin-top:12px;
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
.tag { display:inline-block; padding:2px 8px; border-radius:5px; font-size:11px; font-weight:600;
       background:var(--surface3); color:var(--muted); margin-right:4px; }

/* ── Empty / Loader ── */
.empty { text-align:center; padding:44px 20px; color:var(--muted); }
.empty-icon { font-size:36px; margin-bottom:14px; opacity:0.4; }
.loader { display:flex; align-items:center; justify-content:center; padding:36px; color:var(--muted); font-size:14px; gap:8px; }
.spinner { width:16px; height:16px; border:2px solid var(--border); border-top-color:var(--accent); border-radius:50%; animation:spin 0.7s linear infinite; }
@keyframes spin { to{transform:rotate(360deg)} }
.section-title { font-size:16px; font-weight:700; margin-bottom:14px; }

/* ── Paused badge ── */
.paused-item {
  background:rgba(245,158,11,0.08); border:1px solid rgba(245,158,11,0.2);
  border-radius:8px; padding:7px 10px; font-size:12px; color:var(--warning); margin-bottom:6px;
}

/* ── Improvements ── */
.improvement-card {
  background:var(--surface2);
  border-left:3px solid var(--accent2);
  border-radius:0 10px 10px 0;
  padding:12px; margin-bottom:8px; font-size:13px; line-height:1.55;
}

/* ── Desktop ── */
@media (min-width:640px) {
  body { max-width:760px; margin:0 auto; }
  .section { padding:20px 20px 24px; }
  .stats-grid { grid-template-columns:repeat(4,1fr); }
  .chart-wrap { height:200px; }
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
    <div class="stat-card"><div class="stat-label">Всього постів</div><div class="stat-value" style="color:var(--border)">—</div></div>
    <div class="stat-card"><div class="stat-label">Сер. перегляди</div><div class="stat-value" style="color:var(--border)">—</div></div>
    <div class="stat-card"><div class="stat-label">Всього переглядів</div><div class="stat-value" style="color:var(--border)">—</div></div>
    <div class="stat-card"><div class="stat-label">Топ скор</div><div class="stat-value" style="color:var(--border)">—</div></div>
  </div>
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

<!-- Bottom Navigation -->
<nav class="bottom-nav">
  <div class="nav-item active" data-tab="analytics">
    <div class="nav-bar"></div>
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M18 20V10"/><path d="M12 20V4"/><path d="M6 20v-6"/>
    </svg>
    Аналіз
  </div>
  <div class="nav-item" data-tab="posts">
    <div class="nav-bar"></div>
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
    </svg>
    Пости
  </div>
  <div class="nav-item" data-tab="plan">
    <div class="nav-bar"></div>
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/>
      <line x1="8" y1="18" x2="21" y2="18"/>
      <line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>
    </svg>
    Контент-план
  </div>
  <div class="nav-item" data-tab="videos">
    <div class="nav-bar"></div>
    <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
    </svg>
    Відео
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
        backgroundColor: '#1e1e35',
        borderColor: '#252540',
        borderWidth: 1,
        titleColor: '#f0f0fa',
        bodyColor: '#6b7090',
        padding: 10,
        cornerRadius: 8,
      }
    },
    scales: {
      x: { grid: { color: '#252540' }, ticks: { color: '#6b7090', font: { size: 11 } } },
      y: { grid: { color: '#252540' }, ticks: { color: '#6b7090', font: { size: 11 } } }
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
      <div class="stat-card">
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
        <div style="font-size:13px;line-height:1.65;color:#c0c0d8;margin-bottom:10px">${ins.insights}</div>
        <div class="stat-sub">Оновлено: ${ins.updated_at||'—'} · на основі ${ins.based_on_posts||'?'} постів</div>
      </div>`;
    }

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
        ${ins.post_improvements.split('\n').filter(l=>l.trim()).map(l =>
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
            borderColor: '#6366f1',
            backgroundColor: 'rgba(99,102,241,0.1)',
            borderWidth: 2.5,
            pointBackgroundColor: '#6366f1',
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
let postsLoaded = false;
async function loadPosts() {
  if (postsLoaded) return;
  postsLoaded = true;
  const el = document.getElementById('posts');
  el.innerHTML = loader();
  try {
    const res = await fetch(withToken('/api/posts'));
    if (!res.ok) { el.innerHTML = empty('⚠️', 'Помилка API ' + res.status); return; }
    const data = await res.json();

    const threads = Array.isArray(data.threads) ? data.threads : [];
    const telegram = Array.isArray(data.telegram) ? data.telegram : [];
    const all = [
      ...threads.map(p => ({...p, source:'threads'})),
      ...telegram.map(p => ({...p, source:'telegram'}))
    ].sort((a,b) => (b.timestamp||'').localeCompare(a.timestamp||''));

    if (!all.length) {
      el.innerHTML = empty('📝', 'Постів ще нема');
      return;
    }

    el.innerHTML = all.map(p => {
      const score = engScore(p.metrics);
      return `<div class="card">
        <div class="post-meta">
          <span class="badge-src ${p.source}">${p.source === 'threads' ? 'Threads' : 'Telegram'}</span>
          ${p.type ? `<span class="tag">${p.type}</span>` : ''}
          <span>${p.timestamp || ''}</span>
          ${p.status ? `<span style="color:${p.status==='published'?'var(--success)':'var(--muted)'}">· ${p.status}</span>` : ''}
          ${score ? `<span class="score-badge">${score} pt</span>` : ''}
        </div>
        <div class="post-text">${(p.text || '').slice(0, 320)}</div>
        ${metricPills(p.metrics)}
      </div>`;
    }).join('');
  } catch(err) {
    el.innerHTML = empty('⚠️', 'Помилка: ' + err.message);
  }
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
            <path d="M2 6l3 3 5-5" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
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
async function loadVideos() {
  if (videosLoaded) return;
  videosLoaded = true;
  const el = document.getElementById('videos');
  el.innerHTML = loader();

  const res = await fetch(withToken('/api/videos'));
  const videos = await res.json();

  el.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="insight-label" style="margin-bottom:12px"><div class="dot blue"></div>Додати відео</div>
      <select id="v-platform">
        <option value="threads">Threads</option>
        <option value="tiktok">TikTok</option>
        <option value="reels">Instagram Reels</option>
        <option value="youtube">YouTube Shorts</option>
      </select>
      <input id="v-url" placeholder="Посилання на відео" type="url">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
        <input id="v-views" placeholder="Перегляди" type="number" style="margin:0">
        <input id="v-likes" placeholder="Лайки" type="number" style="margin:0">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
        <input id="v-comments" placeholder="Коментарі" type="number" style="margin:0">
        <input id="v-shares" placeholder="Репости" type="number" style="margin:0">
      </div>
      <button class="btn btn-primary" onclick="addVideo(this)">Додати відео</button>
    </div>
    ${videos.length ? videos.slice().reverse().map(v => `
      <div class="card">
        <div class="post-meta">
          <span class="tag">${v.platform}</span>
          <span>${v.added_at}</span>
          ${v.auto_fetched ? '<span style="color:var(--success)">· авто</span>' : ''}
        </div>
        <a class="video-card-url" href="${v.url}" target="_blank">${v.url}</a>
        ${metricPills({views:v.views, likes:v.likes, replies:v.comments, reposts:v.shares, quotes:v.quotes})}
      </div>
    `).join('') : empty('🎬', 'Відео ще не додано')}
  `;
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

// ── Section loader map ──
const sectionLoaders = {
  analytics: loadAnalytics,
  posts: loadPosts,
  plan: loadPlan,
  videos: loadVideos,
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
