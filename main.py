from openai import OpenAI
import requests
import schedule
import time
import random
import os
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ===== КОНФІГ =====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_USER_ID = os.getenv("THREADS_USER_ID")
LOG_FILE = "posts_log.json"
INSIGHTS_FILE = "style_insights.json"

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
        "prompt": """Напиши пост про те як AI змінює розробку сайтів що це означає для клієнтів.
Не технічно, а людською мовою. Без хайпу. Просто факт з роботи."""
    },
    {
        "type": "value_tip",
        "prompt": """Напиши один конкретний тіп для власника малого бізнесу про сайт.
Щось що можна перевірити або зробити прямо зараз.
Коротко. Без вступу типу 'сьогодні розкажу'."""
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

СТИЛЬ: коротко, від першої особи, як звичайна людина думає вголос. Без висновків і моралі в кінці."""


# ===== ЛОГИ =====
def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_log(posts):
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)


def load_insights():
    if os.path.exists(INSIGHTS_FILE):
        with open(INSIGHTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"insights": None, "updated_at": None}


def save_insights(data):
    with open(INSIGHTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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


def post_to_threads():
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    print(f"\n[{timestamp}] Генерую пост...")

    try:
        text, pillar_type = generate_post()
        print(f"Тип: {pillar_type}")
        print(f"Текст:\n{'-'*40}\n{text}\n{'-'*40}")

        creation_id = create_threads_container(text)
        time.sleep(30)
        post_id = publish_threads_post(creation_id)

        posts = load_log()
        posts.append({
            "timestamp": timestamp,
            "type": pillar_type,
            "text": text,
            "post_id": post_id,
            "status": "published"
        })
        save_log(posts)
        print(f"Опубліковано! ID: {post_id}")

    except Exception as e:
        print(f"Помилка: {e}")
        posts = load_log()
        posts.append({"timestamp": timestamp, "status": "error", "error": str(e)})
        save_log(posts)


def daily_maintenance():
    """Щоденне: підтягує метрики + оновлює аналіз"""
    print("\nЩоденне оновлення метрик...")
    update_metrics()
    analyze_and_learn()


# ===== РОЗКЛАД =====
schedule.every().day.at("06:00").do(post_to_threads)   # 09:00 Київ
schedule.every().day.at("10:30").do(post_to_threads)   # 13:30 Київ
schedule.every().day.at("16:00").do(post_to_threads)   # 19:00 Київ
schedule.every(4).hours.do(daily_maintenance)          # аналіз кожні 4 години

if __name__ == "__main__":
    print("Threads AutoPoster — hodakov.digital")
    print("Розклад: 09:00 / 13:30 / 19:00 (Київ)")
    print("Аналіз: щодня о 06:00")
    print("Перший пост зараз...")
    post_to_threads()

    while True:
        schedule.run_pending()
        time.sleep(60)