"""
Дашборд hodakov.digital — Telegram Mini App для перегляду контент-плану,
історії постів з метриками і таблиці статистики відео.

Запускається як окремий Railway-сервіс (web-процес), окремо від worker'а (main.py),
але читає/пише той самий стан через storage.py (Postgres якщо DATABASE_URL підключений,
інакше json-файли як запасний варіант) — той самий механізм що і в main.py.
"""

from flask import Flask, request, jsonify, render_template_string
from openai import OpenAI
import requests
import os
from datetime import datetime
from storage import load_json, save_json, init_db

# ===== КОНФІГ (ті самі змінні що і в main.py) =====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_USER_ID = os.getenv("THREADS_USER_ID")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN")  # захист від випадкового доступу до URL

LOG_FILE = "posts_log"
TELEGRAM_LOG_FILE = "telegram_log"
INSIGHTS_FILE = "style_insights"
CONTENT_PLAN_FILE = "content_plan"
VIDEO_STATS_FILE = "video_stats"

app = Flask(__name__)
init_db()


def check_token():
    if not DASHBOARD_TOKEN:
        return True  # якщо токен не заданий, доступ відкритий (не рекомендовано для прода)
    return request.args.get("token") == DASHBOARD_TOKEN or request.headers.get("X-Dashboard-Token") == DASHBOARD_TOKEN


@app.before_request
def guard():
    if not check_token():
        return jsonify({"error": "unauthorized"}), 401


# ===== API: пости з метриками =====
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
            idea["done_at"] = datetime.now().strftime("%d.%m.%Y %H:%M") if idea["status"] == "done" else None
    save_json(CONTENT_PLAN_FILE, plan)
    return jsonify({"ok": True})


@app.route("/api/plan/generate", methods=["POST"])
def api_plan_generate():
    count = int(request.json.get("count", 10)) if request.is_json else 10
    insights = load_json(INSIGHTS_FILE, {"insights": None})
    threads_posts = load_json(LOG_FILE, [])
    top_texts = [p["text"] for p in threads_posts if p.get("metrics") and p.get("text")]
    top_texts = top_texts[-10:]

    context = ""
    if insights.get("insights"):
        context += f"Аналіз того що вже добре заходить:\n{insights['insights']}\n\n"
    if top_texts:
        context += "Приклади постів що вже публікувались:\n" + "\n---\n".join(top_texts[:5])

    prompt = f"""Ти генеруєш контент-план для Threads акаунту розробника сайтів @hodakov.digital
(робить сайти з AI за 5-7 днів).

{context}

Згенеруй {count} нових креативних ідей для постів, які спираються на те що вже добре заходило,
але не повторюють один в один старі пости. Ідеї мають бути конкретні, з реальним потенціалом
на охоплення — не загальні теми типу "пост про AI", а конкретна ситуація чи думка яку можна
одразу перетворити в пост.

Формат відповіді — рівно {count} рядків, без нумерації, без зайвого тексту, кожен рядок це одна ідея."""

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


# ===== API: таблиця статистики відео =====
def resolve_threads_metrics(url):
    """Шукає серед власних постів акаунту той що збігається permalink'ом і тягне метрики.
    Повертає dict метрик або None якщо не знайдено / нема доступу."""
    if not THREADS_ACCESS_TOKEN or not THREADS_USER_ID:
        return None
    try:
        list_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads"
        params = {"fields": "id,permalink", "limit": 100, "access_token": THREADS_ACCESS_TOKEN}
        resp = requests.get(list_url, params=params, timeout=10)
        data = resp.json()
        media_id = None
        for item in data.get("data", []):
            if item.get("permalink", "").rstrip("/") == url.rstrip("/"):
                media_id = item["id"]
                break
        if not media_id:
            return None

        insights_url = f"https://graph.threads.net/v1.0/{media_id}/insights"
        insights_params = {"metric": "views,likes,replies,reposts,quotes", "access_token": THREADS_ACCESS_TOKEN}
        insights_resp = requests.get(insights_url, params=insights_params, timeout=10)
        insights_data = insights_resp.json()
        metrics = {}
        for item in insights_data.get("data", []):
            val = item.get("values", [{}])[0].get("value", 0)
            metrics[item["name"]] = val
        return metrics
    except Exception as e:
        print(f"Помилка resolve_threads_metrics: {e}")
        return None


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
            entry["auto_fetched"] = True

    videos = load_json(VIDEO_STATS_FILE, [])
    videos.append(entry)
    save_json(VIDEO_STATS_FILE, videos)
    return jsonify(entry)


@app.route("/api/insights")
def api_insights():
    return jsonify(load_json(INSIGHTS_FILE, {"insights": None}))


# ===== HTML =====
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>hodakov.digital — панель</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  body { background:#0e0e0e; color:#eee; font-family:-apple-system,sans-serif; margin:0; padding:16px; }
  h1 { font-size:18px; margin:0 0 16px; }
  .tabs { display:flex; gap:8px; margin-bottom:16px; }
  .tab { padding:8px 14px; border-radius:8px; background:#1c1c1c; cursor:pointer; font-size:14px; }
  .tab.active { background:#3a7bfd; }
  .card { background:#1a1a1a; border-radius:10px; padding:12px; margin-bottom:10px; }
  .meta { color:#888; font-size:12px; margin-bottom:6px; }
  .metrics { display:flex; gap:12px; font-size:12px; color:#aaa; margin-top:6px; }
  .idea { display:flex; align-items:flex-start; gap:8px; }
  .idea input { margin-top:4px; }
  .idea.done { opacity:0.4; text-decoration:line-through; }
  button.primary { background:#3a7bfd; color:#fff; border:none; border-radius:8px; padding:10px 14px; font-size:14px; width:100%; margin-bottom:12px; }
  input, select, textarea { width:100%; box-sizing:border-box; background:#1c1c1c; color:#eee; border:1px solid #333; border-radius:6px; padding:8px; margin-bottom:8px; font-size:14px; }
  .section { display:none; }
  .section.active { display:block; }
  .insights { white-space:pre-wrap; font-size:13px; color:#ccc; }
</style>
</head>
<body>
<h1>hodakov.digital — панель</h1>
<div class="tabs">
  <div class="tab active" data-tab="posts">Пости</div>
  <div class="tab" data-tab="plan">Контент-план</div>
  <div class="tab" data-tab="videos">Відео</div>
  <div class="tab" data-tab="insights">Аналіз</div>
</div>

<div id="posts" class="section active"></div>
<div id="plan" class="section"></div>
<div id="videos" class="section"></div>
<div id="insights" class="section"></div>

<script>
const tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) tg.expand();

const params = new URLSearchParams(window.location.search);
const token = params.get('token') || '';
function withToken(url) { return url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token); }

document.querySelectorAll('.tab').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(tab.dataset.tab).classList.add('active');
  };
});

function fmtMetrics(m) {
  if (!m) return '';
  return `<div class="metrics">
    <span>👁 ${m.views ?? '-'}</span>
    <span>♥ ${m.likes ?? '-'}</span>
    <span>↺ ${m.reposts ?? '-'}</span>
  </div>`;
}

async function loadPosts() {
  const res = await fetch(withToken('/api/posts'));
  const data = await res.json();
  const el = document.getElementById('posts');
  const all = [...data.threads.map(p => ({...p, source: 'Threads'})),
               ...data.telegram.map(p => ({...p, source: 'Telegram'}))];
  all.sort((a, b) => (b.timestamp || '').localeCompare(a.timestamp || ''));
  el.innerHTML = all.map(p => `
    <div class="card">
      <div class="meta">${p.source} · ${p.type || '?'} · ${p.timestamp || ''} · ${p.status || ''}</div>
      <div>${(p.text || '').slice(0, 300)}</div>
      ${fmtMetrics(p.metrics)}
    </div>
  `).join('') || '<div class="meta">Поки що пусто</div>';
}

async function loadPlan() {
  const res = await fetch(withToken('/api/plan'));
  const ideas = await res.json();
  const el = document.getElementById('plan');
  el.innerHTML = `<button class="primary" onclick="generateIdeas()">Згенерувати нові ідеї</button>` +
    ideas.slice().reverse().map(i => `
      <div class="card idea ${i.status === 'done' ? 'done' : ''}">
        <input type="checkbox" ${i.status === 'done' ? 'checked' : ''} onchange="togglePlan('${i.id}')">
        <div>${i.text}<div class="meta">${i.created_at}</div></div>
      </div>
    `).join('') || '<div class="meta">Ідей ще нема — тисни кнопку вище</div>';
}

async function togglePlan(id) {
  await fetch(withToken(`/api/plan/${id}/toggle`), { method: 'POST' });
  loadPlan();
}

async function generateIdeas() {
  await fetch(withToken('/api/plan/generate'), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({count: 10})
  });
  loadPlan();
}

async function loadVideos() {
  const res = await fetch(withToken('/api/videos'));
  const videos = await res.json();
  const el = document.getElementById('videos');
  el.innerHTML = `
    <div class="card">
      <select id="v-platform"><option value="threads">Threads</option><option value="tiktok">TikTok</option><option value="reels">Instagram Reels</option></select>
      <input id="v-url" placeholder="Посилання на відео">
      <input id="v-views" placeholder="Перегляди (для threads підтягнеться саме)">
      <input id="v-likes" placeholder="Лайки">
      <input id="v-comments" placeholder="Коментарі">
      <input id="v-shares" placeholder="Репости/шери">
      <button class="primary" onclick="addVideo()">Додати</button>
    </div>
  ` + videos.slice().reverse().map(v => `
    <div class="card">
      <div class="meta">${v.platform} · ${v.added_at} ${v.auto_fetched ? '· авто' : ''}</div>
      <div>${v.url}</div>
      <div class="metrics"><span>👁 ${v.views ?? '-'}</span><span>♥ ${v.likes ?? '-'}</span><span>💬 ${v.comments ?? '-'}</span><span>↺ ${v.shares ?? '-'}</span></div>
    </div>
  `).join('');
}

async function addVideo() {
  const body = {
    platform: document.getElementById('v-platform').value,
    url: document.getElementById('v-url').value,
    views: document.getElementById('v-views').value || null,
    likes: document.getElementById('v-likes').value || null,
    comments: document.getElementById('v-comments').value || null,
    shares: document.getElementById('v-shares').value || null
  };
  await fetch(withToken('/api/videos'), {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  loadVideos();
}

async function loadInsights() {
  const res = await fetch(withToken('/api/insights'));
  const data = await res.json();
  document.getElementById('insights').innerHTML =
    `<div class="card insights">${data.insights ? data.insights.replace(/\\n/g, '<br>') : 'Аналізу ще нема — з\\'явиться після 5+ опублікованих постів з метриками'}</div>
     <div class="meta">Оновлено: ${data.updated_at || '-'}</div>`;
}

loadPosts();
loadPlan();
loadVideos();
loadInsights();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
