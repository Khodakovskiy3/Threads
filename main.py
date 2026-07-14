from openai import OpenAI
import requests
import schedule
import time
import random
import os
import json
import hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv
from storage import load_json, save_json, init_db

load_dotenv()

# ===== КОНФІГ =====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_USER_ID = os.getenv("THREADS_USER_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # напр. @hodakov_digital або -100...

# Стан тепер зберігається через storage.py — в Postgres (DATABASE_URL з Railway) якщо він
# підключений, з падінням назад на json-файли якщо ні. Це просто логічні ключі, не шляхи файлів.
LOG_FILE = "posts_log"
INSIGHTS_FILE = "style_insights"
TELEGRAM_LOG_FILE = "telegram_log"
PENDING_CHANNEL_POSTS_FILE = "pending_channel_posts"
VIDEO_STATS_FILE = "video_stats"  # та сама таблиця що заповнює dashboard.py
CONTENT_PLAN_FILE = "content_plan"  # той самий план що і на вкладці dashboard.py

# Постійна клавіатура внизу чату — щоб не пам'ятати команди напам'ять
MAIN_KEYBOARD = {
    "keyboard": [
        ["Панель", "Аналіз"],
        ["Нові ідеї", "Допомога"]
    ],
    "resize_keyboard": True
}

BOT_COMMANDS = [
    {"command": "dashboard", "description": "Відкрити панель (контент-план, пости, відео)"},
    {"command": "analysis", "description": "Останній аналіз постів текстом"},
    {"command": "ideas", "description": "Згенерувати нові ідеї для постів"},
    {"command": "help", "description": "Що вміє бот"},
]

# Типи постів які вважаються "експертними" — саме вони дублюються
# розширеною версією в Telegram і отримують CTA в кінці Threads-поста.
# developer_pain навмисно виключений — це історія, а не лайфхак, туди CTA виглядає недоречно.
EXPERT_TYPES = {"value_tip", "ai_dev"}

TELEGRAM_CTA = "\n\nРозписую детальніше в Telegram: t.me/hodakov_digital"

# ===== ПОШУК ЛІДІВ І САМОРЕКЛАМИ (обхід через Google, поки нема App Review Meta) =====
GOOGLE_SEARCH_API_KEY = os.getenv("GOOGLE_SEARCH_API_KEY")
GOOGLE_SEARCH_CX = os.getenv("GOOGLE_SEARCH_CX")
TELEGRAM_USER_CHAT_ID = os.getenv("TELEGRAM_USER_CHAT_ID")  # особистий чат з ботом (не канал)
DASHBOARD_URL = os.getenv("DASHBOARD_URL")    # публічний домен сервісу dashboard.py на Railway
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN")  # той самий токен що і в dashboard.py

SEEN_LEADS_FILE = "seen_leads"
SEEN_SELFPROMO_FILE = "seen_selfpromo"
PENDING_REPLIES_FILE = "pending_replies"
TELEGRAM_OFFSET_FILE = "telegram_offset"
REPLY_TEMPLATE_STATE_FILE = "reply_template_state"

LEAD_KEYWORDS = [
    "потрібен розробник сайту",
    "шукаю розробника сайту",
    "вакансія розробник сайту",
    "нужен разработчик сайта",
    "ищу разработчика сайта",
    "looking for a web developer",
]

SELFPROMO_KEYWORDS = [
    "роблю сайти",
    "розробляю сайти під ключ",
    "дизайн і розробка сайту",
    "делаю сайты под ключ",
    "web developer for hire",
]

# Готові відповіді під саморекламні тредси. Чергуються по колу — GPT тут не викликається,
# щоб не палити токени на кожен збіг.
REPLY_TEMPLATES = [
    "Роблю такі сайти під ключ за тиждень, з дизайном і кодом одразу. Можу показати приклади якщо цікаво.",
    "У мене якраз професія на цьому. Роблю сайт від дизайну до запуску за 5-7 днів, без місяців очікування.",
    "Бачив подібні запити раніше. Роблю сайти сам, від макету до сервера, швидко і без посередників.",
]

# ===== КОНТЕНТ ПЛАН =====
CONTENT_PILLARS = [
    {
        "type": "storytelling_client",
        "prompt": """Напиши короткий смішний або впізнаваний момент з роботи розробника сайтів.
Обов'язково ситуація з клієнтом. Щось що зрозуміє будь-яка людина навіть далека від IT.
Від першої особи. Без повчань і висновків в кінці."""
    },
    {
        "type": "observation_business",
        "prompt": """Напиши коротке спостереження про малий бізнес і сайти.
Щось що змусить власника кав'ярні, салону або курсів впізнати свою ситуацію.
Без реклами. Просто як 'це про мене' момент."""
    },
    {
        "type": "question_engagement",
        "prompt": """Напиши одне просте питання для людей які мають або планують бізнес.
Про сайт, про довіру клієнтів, про те як вони знаходять послуги в інтернеті.
Питання має бути таке що хочеться відповісти в коментарі."""
    },
    {
        "type": "developer_pain",
        "prompt": """Напиши пост про типову ситуацію між клієнтом і розробником.
Розробник кинув проект, тягнув місяцями, зробив не те.
Без злості, просто як факт. Коротко."""
    },
    {
        "type": "ai_dev",
        "prompt": """Напиши конкретний лайфхак з власної розробки на основі AI-інструментів
(Claude, Cursor, ChatGPT). Це має бути прийом який можна застосувати прямо зараз:
як економити токени, який промт реально працює краще, який інструмент рятує час,
як побудувати процес щоб AI менше плутався. Без загальних роздумів про 'майбутнє AI'
чи 'як AI змінює індустрію'. Один конкретний прийом, по суті, як людина ділиться
знахідкою з роботи."""
    },
    {
        "type": "value_tip",
        "prompt": """Напиши один конкретний тіп для власника малого бізнесу про сайт.
Щось що можна перевірити або зробити прямо зараз.
Дай пораду через особистий досвід чи конкретний випадок, а не через абстрактну статистику
типу 'X% користувачів роблять Y'. Коротко. Без вступу типу 'сьогодні розкажу'."""
    }
]

BASE_SYSTEM_PROMPT = """Ти пишеш пости для Threads від імені Богдана Ходакова (@hodakov.digital).
Богдан розробляє сайти і додатки з AI за 5-7 днів від дизайну до запуску.

ЖОРСТКО ЗАБОРОНЕНО:
- Тире як пунктуація
- Слова: критично, важливо, ключовий, унікальний, рішення, підхід, результат, онлайн-присутність
- Списки
- Хештеги і заклики підписатись
- Повчальні висновки типу "це важливо для бізнесу"
- "Не X, а Y" конструкції
- Емодзі

ЩЕ ЗАБОРОНЕНО (типові AI-тики, які видають що текст писала не людина):
- Риторичний "мудрий" фінал що узагальнює весь пост в одну красиву думку.
  Приклади того що НЕ можна писати: "Це ж як по-новому дихати", "Виглядає так, як і має бути, хорошим",
  "Ось так і живемо", "І це найкраще що могло статись". Пост може просто обірватись на факті чи деталі,
  без фінального висновку що "закриває" тему.
- Гола статистика чи узагальнене твердження без прив'язки до конкретної ситуації
  (типу "50% відвідувачів закривають сайт" як абстрактний факт). Якщо є цифра, вона має бути частиною
  реальної історії чи конкретної дії, а не маркетинговим твердженням.
- Кінематографічні вступи що "малюють сцену": "Ранок почався з...", "Уявіть...", "Сьогодні я прокинувся і...".
  Починай одразу з суті, як людина яка просто пише думку в телефон.
- Ідеально гладка структура. Живий текст має шорсткість: можна обірвати речення, повторити слово,
  поправити себе на ходу ("хоча ні, точніше..."), лишити думку не зовсім завершеною.
- Занадто чиста граматична симетрія (три однакові за формою речення поспіль). Людина так не пише.

ПРИКЛАДИ ХОРОШИХ ПОСТІВ:

Приклад 1:
"Знайомий написав восени. Треба сайтик, нічого складного, просто щоб було.
Я уточнив що саме. Порахував. Назвав ціну.
Він відповів "окей зрозумів".
Просто "окей зрозумів" це такий спосіб попрощатись назавжди.
Тепер ми просто знайомі."

Приклад 2:
"Клієнт написав о 23:41.
Ти не міг би зробити щоб кнопка виглядала трохи... інакше?
Питаю що саме змінити.
Ну от є таке відчуття. Сам зрозумієш.
На восьмому варіанті він пише: О, оцей! Бачиш, ти одразу зрозумів що я мав на увазі."

Приклад 3:
"Ви реально читаєте текст на сайтах чи одразу шукаєте кнопку?"

Приклад 4:
"Найстрашніша фраза від клієнта: зроби щоб було красиво.
Не приклади. Не референси. Просто. Красиво."

ПРИКЛАД ПОГАНОГО ПОСТА (так писати не можна, це і є AI-тон):
"Ранок почався зі звичайного запиту на сайт. Спершу здавалось що нічого нового, але тепер AI вміє
доповнювати мої думки. Раніше на це йшло б години дві. Можна обговорити все відразу і не чекати тижнями.
Це ж як по-новому дихати."
Проблема: кінематографічний вступ, штучний висновок в кінці, занадто рівна структура речень.

СТИЛЬ: коротко, від першої особи, як звичайна людина думає вголос і пише в телефоні між справами.
Без висновків і моралі в кінці. Пост може закінчитись на середині думки чи на конкретній репліці,
а не на красивому узагальненні."""


# ===== ЛОГИ =====
def load_log():
    return load_json(LOG_FILE, [])


def save_log(posts):
    save_json(LOG_FILE, posts)


def load_insights():
    return load_json(INSIGHTS_FILE, {"insights": None, "updated_at": None})


def save_insights(data):
    save_json(INSIGHTS_FILE, data)


# ===== МЕТРИКИ =====
def fetch_post_metrics(post_id):
    """Отримує перегляди, лайки, репости для поста"""
    url = f"https://graph.threads.net/v1.0/{post_id}/insights"
    params = {
        "metric": "views,likes,replies,reposts,quotes",
        "access_token": THREADS_ACCESS_TOKEN
    }
    response = requests.get(url, params=params)
    data = response.json()

    metrics = {}
    for item in data.get("data", []):
        val = item.get("values", [{}])[0].get("value", 0)
        metrics[item["name"]] = val
    return metrics


def update_metrics():
    """Підтягує метрики для постів старше 24 годин"""
    posts = load_log()
    updated = False

    for post in posts:
        if post.get("status") != "published":
            continue
        if post.get("metrics"):
            continue

        try:
            post_time = datetime.strptime(post["timestamp"], "%d.%m.%Y %H:%M")
        except:
            continue

        if datetime.now() - post_time < timedelta(hours=3):
            continue

        try:
            metrics = fetch_post_metrics(post["post_id"])
            if metrics:
                post["metrics"] = metrics
                updated = True
                print(f"Метрики оновлено для поста {post['post_id']}: {metrics}")
        except Exception as e:
            print(f"Помилка метрик: {e}")

    if updated:
        save_log(posts)


# ===== АНАЛІЗ І НАВЧАННЯ =====
def analyze_and_learn():
    """Аналізує які пости працюють краще і оновлює рекомендації"""
    posts = load_log()
    posts_with_metrics = [
        p for p in posts
        if p.get("metrics") and p.get("text") and p.get("status") == "published"
    ]

    if len(posts_with_metrics) < 5:
        print(f"Недостатньо даних для аналізу ({len(posts_with_metrics)}/5 постів)")
        return

    sorted_posts = sorted(
        posts_with_metrics,
        key=lambda x: x["metrics"].get("views", 0),
        reverse=True
    )

    top = sorted_posts[:3]
    bottom = sorted_posts[-3:]

    def format_post(p):
        m = p.get("metrics", {})
        return (
            f"Тип: {p.get('type', '?')} | "
            f"Перегляди: {m.get('views', 0)} | "
            f"Лайки: {m.get('likes', 0)} | "
            f"Репости: {m.get('reposts', 0)}\n"
            f"Текст: {p['text']}"
        )

    prompt = f"""Проаналізуй пости Threads акаунту розробника сайтів @hodakov.digital.

ТОП ПОСТИ (найбільше переглядів):
{chr(10).join([format_post(p) for p in top])}

СЛАБКІ ПОСТИ (найменше переглядів):
{chr(10).join([format_post(p) for p in bottom])}

Дай 3-5 коротких конкретних висновків:
- Які теми і формати дають більше охоплення
- Що треба уникати
- Що конкретно змінити в стилі

Відповідай по-українськи. Тільки конкретні спостереження, без загальних порад."""

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )

    insights_text = response.choices[0].message.content.strip()

    save_insights({
        "insights": insights_text,
        "updated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "based_on_posts": len(posts_with_metrics)
    })

    print(f"\nАналіз оновлено на основі {len(posts_with_metrics)} постів:")
    print(insights_text)


# ===== ГЕНЕРАЦІЯ ПОСТІВ =====
def build_system_prompt():
    """Будує промпт з урахуванням накопичених інсайтів"""
    prompt = BASE_SYSTEM_PROMPT

    insights_data = load_insights()
    if insights_data.get("insights"):
        prompt += f"""

АНАЛІЗ ПОПЕРЕДНІХ ПОСТІВ (що реально працює для цього акаунту):
{insights_data['insights']}

Враховуй ці спостереження при написанні нового поста."""

    return prompt


def generate_post():
    pillar = random.choice(CONTENT_PILLARS)
    system_prompt = build_system_prompt()

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=400,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": pillar["prompt"]}
        ]
    )

    text = response.choices[0].message.content.strip()
    return text, pillar["type"]


# ===== TELEGRAM (кросспост експертних постів) =====
def generate_telegram_post(threads_text, pillar_type):
    """Розширює короткий експертний Threads-пост у повноцінний Telegram-пост."""
    system_prompt = """Ти пишеш пост для Telegram каналу Богдана Ходакова (@hodakov_digital),
розробника сайтів і додатків. Це той самий канал куди Богдан веде людей з Threads.

Тобі дають короткий пост з Threads. Розпиши цю саму думку ширше і конкретніше:
додай реальний приклад, конкретну деталь або крок, який людина може забрати собі.
Це має відчуватись як продовження думки, а не переказ того самого поста.

ЖОРСТКО ЗАБОРОНЕНО:
- Тире як пунктуація
- Слова: критично, важливо, ключовий, унікальний, рішення, підхід, результат, онлайн-присутність
- Списки
- Хештеги
- Емодзі
- Повчальні висновки в кінці
- Риторичний "мудрий" фінал що узагальнює весь текст в одну красиву думку
  (типу "ось так і живемо", "це і є справжня цінність")
- Гола статистика чи узагальнене твердження без прив'язки до конкретної ситуації
- Кінематографічні вступи типу "Ранок почався з...", "Уявіть..."
- Занадто гладка, ідеально симетрична структура речень

СТИЛЬ: коротко, від першої особи, як звичайна людина думає вголос і пише між справами.
Текст може обірватись на конкретній деталі, без фінального узагальнення."""

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=500,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Ось пост з Threads:\n\n{threads_text}"}
        ]
    )
    return response.choices[0].message.content.strip()


def publish_to_telegram(text, photo_file_id=None):
    """Публікує в Telegram-канал. Якщо є фото — постить його з підписом (caption),
    якщо текст довший за ліміт підпису (1024 символи) — шле фото і текст окремим повідомленням."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        print("Telegram не налаштований (немає TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID) — пропускаю")
        return None

    if photo_file_id:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        fits_caption = len(text) <= 1024
        params = {
            "chat_id": TELEGRAM_CHANNEL_ID,
            "photo": photo_file_id,
            "caption": text if fits_caption else ""
        }
        response = requests.post(url, params=params)
        data = response.json()
        if not data.get("ok"):
            raise Exception(f"Помилка публікації в Telegram: {data}")
        message_id = data["result"]["message_id"]

        if not fits_caption:
            extra_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(extra_url, params={"chat_id": TELEGRAM_CHANNEL_ID, "text": text})

        return message_id

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    params = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text
    }
    response = requests.post(url, params=params)
    data = response.json()
    if not data.get("ok"):
        raise Exception(f"Помилка публікації в Telegram: {data}")
    return data["result"]["message_id"]


def load_telegram_log():
    return load_json(TELEGRAM_LOG_FILE, [])


def save_telegram_log(posts):
    save_json(TELEGRAM_LOG_FILE, posts)


def crosspost_to_telegram(threads_text, pillar_type):
    """Якщо пост експертного типу (лайфхак/тіп) — генерує розширену версію і шле власнику
    в Telegram на перевірку з кнопками Опублікувати/Скасувати. Ніякої автопублікації в канал —
    можна ще прикріпити фото (reply фоткою на це повідомлення) перед публікацією."""
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    try:
        tg_text = generate_telegram_post(threads_text, pillar_type)
        print(f"\nTelegram-пост на перевірку (тип {pillar_type}):\n{'-'*40}\n{tg_text}\n{'-'*40}")

        short_id = hashlib.md5((threads_text + timestamp).encode()).hexdigest()[:10]

        message = (
            f"Готовий пост для Telegram-каналу (тип {pillar_type})\n\n"
            f"{tg_text}\n\n"
            f"Якщо треба фото — зроби reply на це повідомлення фоткою, потім тисни Опублікувати."
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "Опублікувати", "callback_data": f"pub:{short_id}"},
                {"text": "Скасувати", "callback_data": f"cancel:{short_id}"}
            ]]
        }
        prompt_message_id = send_telegram_dm(message, reply_markup=keyboard)

        pending_channel = load_json(PENDING_CHANNEL_POSTS_FILE, {})
        pending_channel[short_id] = {
            "type": pillar_type,
            "text": tg_text,
            "prompt_message_id": prompt_message_id,
            "photo_file_id": None,
            "timestamp": timestamp
        }
        save_json(PENDING_CHANNEL_POSTS_FILE, pending_channel)

        posts = load_telegram_log()
        posts.append({
            "timestamp": timestamp,
            "type": pillar_type,
            "text": tg_text,
            "status": "pending_review"
        })
        save_telegram_log(posts)

    except Exception as e:
        alert_error("Telegram crosspost", e)
        posts = load_telegram_log()
        posts.append({"timestamp": timestamp, "type": pillar_type, "status": "error", "error": str(e)})
        save_telegram_log(posts)


# ===== ПУБЛІКАЦІЯ =====
def create_threads_container(text):
    url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads"
    params = {
        "media_type": "TEXT",
        "text": text,
        "access_token": THREADS_ACCESS_TOKEN
    }
    response = requests.post(url, params=params)
    data = response.json()
    if "id" not in data:
        raise Exception(f"Помилка створення: {data}")
    return data["id"]


def publish_threads_post(creation_id):
    url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_publish"
    params = {
        "creation_id": creation_id,
        "access_token": THREADS_ACCESS_TOKEN
    }
    response = requests.post(url, params=params)
    data = response.json()
    if "id" not in data:
        raise Exception(f"Помилка публікації: {data}")
    return data["id"]


KYIV_UTC_OFFSET_HOURS = 3  # без урахування переходів на зимовий/літній час — як і було в проєкті

# Вікна публікацій по Києву: (година_від, година_до, інтервал_годин_між_постами).
# 23:00-06:00 навмисно відсутнє в списку — в цей час не постимо взагалі.
POSTING_WINDOWS = [
    (6, 12, 2),   # 06:00-12:00 — 1 пост на 2 години
    (12, 16, 1),  # 12:00-16:00 — 1 пост на годину
    (16, 21, 2),  # 16:00-21:00 — 1 пост на 2 години
    (21, 23, 2),  # 21:00-23:00 — 1 пост (інтервал 2 год в 2-годинному вікні = максимум один)
]


def kyiv_now():
    return datetime.utcnow() + timedelta(hours=KYIV_UTC_OFFSET_HOURS)


def desired_interval_hours(hour):
    for start, end, interval in POSTING_WINDOWS:
        if start <= hour < end:
            return interval
    return None  # 23:00-06:00


def hours_since_last_post():
    posts = [p for p in load_log() if p.get("status") == "published" and p.get("timestamp")]
    if not posts:
        return None
    last = max(posts, key=lambda p: datetime.strptime(p["timestamp"], "%d.%m.%Y %H:%M"))
    last_time = datetime.strptime(last["timestamp"], "%d.%m.%Y %H:%M")
    return (datetime.now() - last_time).total_seconds() / 3600


def post_to_threads():
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    kyiv_hour = kyiv_now().hour

    interval = desired_interval_hours(kyiv_hour)
    if interval is None:
        print(f"\n[{timestamp}] Зараз {kyiv_hour}:00 за Києвом — вікно 23:00-06:00, не постимо")
        return

    hours_since = hours_since_last_post()
    if hours_since is not None and hours_since < interval:
        print(f"\n[{timestamp}] Останній пост був {hours_since:.1f} год тому, для {kyiv_hour}:00 "
              f"потрібно {interval} год між постами — пропускаю")
        return

    print(f"\n[{timestamp}] Генерую пост...")

    try:
        text, pillar_type = generate_post()
        is_expert = pillar_type in EXPERT_TYPES
        published_text = text + TELEGRAM_CTA if is_expert else text

        print(f"Тип: {pillar_type} {'(експертний → CTA + Telegram)' if is_expert else ''}")
        print(f"Текст:\n{'-'*40}\n{published_text}\n{'-'*40}")

        creation_id = create_threads_container(published_text)
        time.sleep(30)
        post_id = publish_threads_post(creation_id)

        posts = load_log()
        posts.append({
            "timestamp": timestamp,
            "type": pillar_type,
            "text": published_text,
            "post_id": post_id,
            "status": "published"
        })
        save_log(posts)
        print(f"Опубліковано! ID: {post_id}")

        if is_expert:
            crosspost_to_telegram(text, pillar_type)

    except Exception as e:
        alert_error("publish Threads post", e)
        posts = load_log()
        posts.append({"timestamp": timestamp, "status": "error", "error": str(e)})
        save_log(posts)


def daily_maintenance():
    """Щоденне: підтягує метрики + оновлює аналіз"""
    print("\nЩоденне оновлення метрик...")
    try:
        update_metrics()
        analyze_and_learn()
    except Exception as e:
        alert_error("daily_maintenance (метрики/аналіз)", e)


def answer_bot_question(question):
    """Відповідає на довільне питання власника (напр. 'куди рухатись з відео'),
    спираючись на реальні дані акаунту: топ/слабкі пости, статистику відео, попередній аналіз.
    Не генерує загальних порад — якщо даних мало, чесно каже що бракує."""
    try:
        posts = load_log()
        with_metrics = [p for p in posts if p.get("status") == "published" and p.get("metrics")]
        video_stats = load_json(VIDEO_STATS_FILE, [])
        insights = load_insights()

        if not with_metrics and not video_stats:
            send_telegram_dm(
                "Поки що замало даних (нема опублікованих постів з метриками чи доданих відео в "
                "таблицю), щоб відповісти конкретно. Додай пару відео в панель (/dashboard) або "
                "почекай поки набіжать перегляди на пости — і питай знову."
            )
            return

        def fmt_post(p):
            m = p.get("metrics", {})
            return (f"[{p.get('type', '?')}] перегляди {m.get('views', 0)}, лайки {m.get('likes', 0)}, "
                    f"репости {m.get('reposts', 0)}: {p.get('text', '')[:200]}")

        def fmt_video(v):
            return (f"[{v.get('platform')}] перегляди {v.get('views', '-')}, лайки {v.get('likes', '-')}, "
                    f"коментарі {v.get('comments', '-')}: {v.get('url', '')}")

        sorted_posts = sorted(with_metrics, key=lambda p: p["metrics"].get("views", 0), reverse=True)

        context = ""
        if sorted_posts:
            context += "ТОП ПОСТИ ЗА ОХОПЛЕННЯМ:\n" + "\n".join(fmt_post(p) for p in sorted_posts[:5])
            context += "\n\nСЛАБКІ ПОСТИ:\n" + "\n".join(fmt_post(p) for p in sorted_posts[-5:])
        if video_stats:
            context += "\n\nВІДЕО ЯКІ ВЖЕ ПУБЛІКУВАЛИСЬ:\n" + "\n".join(fmt_video(v) for v in video_stats[-15:])
        if insights.get("insights"):
            context += f"\n\nПОПЕРЕДНІЙ АНАЛІЗ ПОСТІВ:\n{insights['insights']}"

        prompt = f"""Ти аналітик контенту для Threads і відео (TikTok/Reels/Threads) акаунту
розробника сайтів @hodakov.digital.

{context}

Питання власника акаунту: {question}

Дай конкретну відповідь спираючись тільки на ці реальні дані, без загальних порад типу
'знімайте більше відео' чи 'будьте автентичним' без прив'язки до того що показують цифри.
Якщо даних замало щоб впевнено відповісти на щось конкретне в питанні — чесно скажи що саме
бракує (наприклад мало відео в таблиці, чи мало постів з метриками)."""

        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        send_telegram_dm(response.choices[0].message.content.strip())

    except Exception as e:
        alert_error("відповідь на питання в боті", e)


def generate_content_ideas(count=10):
    """Генерує нові ідеї для контент-плану на основі попереднього аналізу і топових постів.
    Дописує в CONTENT_PLAN_FILE (та сама таблиця що на вкладці 'Контент-план' в /dashboard)
    і повертає список нових текстів ідей."""
    insights = load_insights()
    posts = load_log()
    top_texts = [p["text"] for p in posts if p.get("metrics") and p.get("text")][-10:]

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
    return lines


HELP_TEXT = (
    "Що вміє бот:\n\n"
    "Пости в Threads публікуються самі по розкладу (06-12 раз на 2 год, 12-16 щогодини, "
    "16-21 раз на 2 год, 21-23 один пост, 23-06 тиша).\n\n"
    "Панель — контент-план, історія постів з метриками, таблиця відео.\n"
    "Аналіз — останній аналіз того що добре заходить, текстом прямо в чат.\n"
    "Нові ідеї — згенерувати ще ідей для контент-плану.\n\n"
    "Просто питання текстом (без /) — бот відповість спираючись на реальну статистику акаунту.\n\n"
    "Сповіщення про лідів і саморекламні тредси, а також запити на публікацію в Telegram-канал "
    "приходять самі, з кнопками підтвердження."
)


# ===== TELEGRAM DM (сповіщення власнику, не канал) =====
def send_telegram_dm(text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_USER_CHAT_ID:
        print("Telegram DM не налаштований (TELEGRAM_BOT_TOKEN / TELEGRAM_USER_CHAT_ID)")
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_USER_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    try:
        response = requests.post(url, data=payload)
        data = response.json()
        if not data.get("ok"):
            print(f"Помилка Telegram DM: {data}")
            return None
        return data["result"]["message_id"]
    except Exception as e:
        print(f"Помилка Telegram DM: {e}")
        return None


def alert_error(context, error):
    """Друкує помилку в лог І шле власнику в Telegram, щоб не пропустити збій пайплайну."""
    print(f"Помилка [{context}]: {error}")
    send_telegram_dm(f"Помилка: {context}\n\n{error}")


def answer_callback_query(callback_id, text=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    params = {"callback_query_id": callback_id}
    if text:
        params["text"] = text
    try:
        requests.post(url, params=params)
    except Exception:
        pass


def register_bot_commands():
    """Реєструє список команд в меню '/' Telegram — викликати один раз при старті."""
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    try:
        requests.post(url, json={"commands": BOT_COMMANDS})
    except Exception as e:
        print(f"Не вдалось зареєструвати команди бота: {e}")


def send_dashboard_button():
    if not DASHBOARD_URL:
        send_telegram_dm("Дашборд ще не налаштований (немає DASHBOARD_URL)")
        return
    dash_url = DASHBOARD_URL
    if DASHBOARD_TOKEN:
        dash_url += f"?token={DASHBOARD_TOKEN}"
    keyboard = {"inline_keyboard": [[{"text": "Відкрити панель", "web_app": {"url": dash_url}}]]}
    send_telegram_dm("Контент-план, історія постів і статистика відео:", reply_markup=keyboard)


# ===== ПОШУК ЛІДІВ І САМОРЕКЛАМИ ЧЕРЕЗ GOOGLE =====
def google_search(query, num=10):
    """Пошук по публічному Google-індексу threads.net.
    Тимчасовий обхід поки не схвалено App Review на threads_keyword_search в Meta —
    після схвалення треба замінити на офіційний /keyword_search ендпоінт Threads API."""
    if not GOOGLE_SEARCH_API_KEY or not GOOGLE_SEARCH_CX:
        print("Google Search не налаштований (GOOGLE_SEARCH_API_KEY / GOOGLE_SEARCH_CX)")
        return []

    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_SEARCH_API_KEY,
        "cx": GOOGLE_SEARCH_CX,
        "q": query,
        "num": num
    }
    response = requests.get(url, params=params)
    data = response.json()
    return data.get("items", [])


def build_or_query(keywords):
    quoted = " OR ".join(f'"{k}"' for k in keywords)
    return f"site:threads.net ({quoted})"


def get_next_template():
    state = load_json(REPLY_TEMPLATE_STATE_FILE, {"index": 0})
    idx = state["index"] % len(REPLY_TEMPLATES)
    template = REPLY_TEMPLATES[idx]
    state["index"] = idx + 1
    save_json(REPLY_TEMPLATE_STATE_FILE, state)
    return template


def search_leads():
    """Шукає тредси де хтось пише що потрібен розробник сайту / відкрита вакансія.
    Тільки сповіщення в Telegram, без автовідповіді — відповідати треба самому і швидко."""
    seen = load_json(SEEN_LEADS_FILE, [])
    query = build_or_query(LEAD_KEYWORDS)

    try:
        items = google_search(query)
    except Exception as e:
        alert_error("пошук лідів", e)
        return

    new_seen = seen[:]
    for item in items:
        link = item.get("link")
        if not link or link in seen:
            continue

        snippet = item.get("snippet", "")
        message = (
            f"Можливий клієнт на Threads\n\n"
            f"{snippet}\n\n"
            f"{link}\n\n"
            f"Відповідай сам і швидко, поки тред живий."
        )
        send_telegram_dm(message)
        new_seen.append(link)

    if new_seen != seen:
        save_json(SEEN_LEADS_FILE, new_seen)


def search_selfpromo():
    """Шукає тредси де хтось постить свої послуги розробки сайтів.
    Готує чергову шаблонну відповідь і шле в Telegram на підтвердження —
    ніякої автопублікації без дозволу власника."""
    seen = load_json(SEEN_SELFPROMO_FILE, [])
    pending = load_json(PENDING_REPLIES_FILE, {})
    query = build_or_query(SELFPROMO_KEYWORDS)

    try:
        items = google_search(query)
    except Exception as e:
        alert_error("пошук самореклами", e)
        return

    new_seen = seen[:]
    for item in items:
        link = item.get("link")
        if not link or link in seen:
            continue

        snippet = item.get("snippet", "")
        template = get_next_template()
        short_id = hashlib.md5(link.encode()).hexdigest()[:10]
        pending[short_id] = {"link": link, "reply": template}

        message = (
            f"Тред з саморекламою послуг\n\n"
            f"{snippet}\n\n"
            f"{link}\n\n"
            f"Готова відповідь (тапни щоб скопіювати і встав в Threads):\n"
            f"<code>{template}</code>"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "Відправлено", "callback_data": f"done:{short_id}"},
                {"text": "Пропустити", "callback_data": f"skip:{short_id}"}
            ]]
        }
        send_telegram_dm(message, reply_markup=keyboard)
        new_seen.append(link)

    if new_seen != seen:
        save_json(SEEN_SELFPROMO_FILE, new_seen)
    save_json(PENDING_REPLIES_FILE, pending)


def poll_telegram_updates():
    """Перевіряє:
    1) фото-реплаї на прев'ю Telegram-поста — прикріплює фото до pending_channel_posts
    2) натискання кнопок:
       - done/skip — для готових відповідей під саморекламні тредси (pending_replies)
       - pub/cancel — для публікації/скасування Telegram-крос-поста (pending_channel_posts)
    """
    if not TELEGRAM_BOT_TOKEN:
        return

    offset_data = load_json(TELEGRAM_OFFSET_FILE, {"offset": 0})
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": offset_data["offset"], "timeout": 0}

    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
    except Exception as e:
        print(f"Помилка отримання Telegram updates: {e}")
        return

    if not data.get("ok"):
        return

    pending_replies = load_json(PENDING_REPLIES_FILE, {})
    pending_channel = load_json(PENDING_CHANNEL_POSTS_FILE, {})
    max_update_id = offset_data["offset"] - 1

    for update in data.get("result", []):
        max_update_id = max(max_update_id, update["update_id"])

        message = update.get("message")
        text = message.get("text", "").strip() if message else ""

        # /start — вітання + постійна клавіатура з кнопками замість команд напам'ять
        if text == "/start":
            send_telegram_dm(
                "Готово, бот на зв'язку. Знизу є кнопки для швидкого доступу, або просто питай текстом.",
                reply_markup=MAIN_KEYBOARD
            )
            continue

        # Панель — команда /dashboard або кнопка клавіатури
        if text in ("/dashboard", "Панель"):
            send_dashboard_button()
            continue

        # Аналіз — останній style_insights.json прямо текстом в чат
        if text in ("/analysis", "Аналіз"):
            insights = load_insights()
            if insights.get("insights"):
                send_telegram_dm(f"Аналіз (оновлено {insights.get('updated_at', '?')}):\n\n{insights['insights']}")
            else:
                send_telegram_dm("Аналізу ще нема — з'явиться після 5+ опублікованих постів з метриками.")
            continue

        # Нові ідеї — генерує і одразу показує текстом (та ж таблиця що і в /dashboard)
        if text in ("/ideas", "Нові ідеї"):
            try:
                ideas = generate_content_ideas(10)
                listed = "\n".join(f"{i+1}. {idea}" for i, idea in enumerate(ideas))
                send_telegram_dm(f"Нові ідеї (додані в контент-план):\n\n{listed}")
            except Exception as e:
                alert_error("генерація ідей з бота", e)
            continue

        # Допомога
        if text in ("/help", "Допомога"):
            send_telegram_dm(HELP_TEXT)
            continue

        # фото як reply на прев'ю поста — прикріплюємо до відповідного pending запису
        if message and message.get("photo") and message.get("reply_to_message"):
            replied_id = message["reply_to_message"]["message_id"]
            for short_id, item in pending_channel.items():
                if item.get("prompt_message_id") == replied_id:
                    item["photo_file_id"] = message["photo"][-1]["file_id"]
                    send_telegram_dm("Фото додано до цього поста. Тисни Опублікувати коли готово.")
                    break

        # довільний текст без фото і без "/" на початку — трактуємо як питання про контент/стратегію
        if text and not (message and message.get("photo")):
            answer_bot_question(text)
            continue

        callback = update.get("callback_query")
        if not callback:
            continue

        data_str = callback.get("data", "")
        callback_id = callback["id"]

        if ":" not in data_str:
            continue

        action, short_id = data_str.split(":", 1)

        if action in ("done", "skip"):
            if short_id in pending_replies:
                pending_replies.pop(short_id, None)
                answer_text = "Відмічено як відправлене" if action == "done" else "Пропущено"
            else:
                answer_text = "Вже оброблено"
            answer_callback_query(callback_id, answer_text)

        elif action in ("pub", "cancel"):
            if short_id in pending_channel:
                item = pending_channel.pop(short_id)
                if action == "pub":
                    try:
                        publish_to_telegram(item["text"], item.get("photo_file_id"))
                        answer_text = "Опубліковано в каналі"
                    except Exception as e:
                        answer_text = f"Помилка публікації: {e}"
                        print(answer_text)
                else:
                    answer_text = "Скасовано"
            else:
                answer_text = "Вже оброблено"
            answer_callback_query(callback_id, answer_text)

    save_json(PENDING_REPLIES_FILE, pending_replies)
    save_json(PENDING_CHANNEL_POSTS_FILE, pending_channel)
    save_json(TELEGRAM_OFFSET_FILE, {"offset": max_update_id + 1})


# ===== РОЗКЛАД =====
schedule.every(30).minutes.do(post_to_threads)         # перевірка вікна публікацій (див. POSTING_WINDOWS)
schedule.every(4).hours.do(daily_maintenance)          # аналіз кожні 4 години
schedule.every(30).minutes.do(search_leads)            # пошук лідів (потрібен розробник)
schedule.every(30).minutes.do(search_selfpromo)        # пошук самореклами конкурентів
schedule.every(1).minutes.do(poll_telegram_updates)    # перевірка натискань кнопок

if __name__ == "__main__":
    print("Threads AutoPoster — hodakov.digital")
    print("Розклад по Києву: 06-12 раз на 2год, 12-16 щогодини, 16-21 раз на 2год, 21-23 один пост, 23-06 тиша")
    init_db()
    register_bot_commands()
    print("Перевірка при старті (пропускається якщо не в вікні або останній пост був недавно)...")
    post_to_threads()

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            # запобіжник: жодна окрема задача не повинна вбивати весь воркер
            alert_error("schedule.run_pending", e)
        time.sleep(60)