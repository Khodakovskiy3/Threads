from openai import OpenAI
import requests
import schedule
import time
import random
import os
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ===== КОНФІГ =====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_USER_ID = os.getenv("THREADS_USER_ID")
LOG_FILE = "posts_log.json"

# ===== КОНТЕНТ ПЛАН =====
CONTENT_PILLARS = [
    {
        "type": "storytelling_client",
        "prompt": """Напиши короткий смішний або впізнаваний момент з роботи розробника сайтів.
Обов'язково — ситуація з клієнтом. Щось що зрозуміє будь-яка людина навіть далека від IT.
Наприклад: клієнт просить 'зроби красиво', 'як в Apple але дешевше', або кидає проект і повертається.
Від першої особи. Без повчань і висновків в кінці."""
    },
    {
        "type": "observation_business",
        "prompt": """Напиши коротке спостереження про малий бізнес і сайти або онлайн присутність.
Щось що змусить власника кав'ярні, салону або курсів впізнати свою ситуацію.
Без реклами. Просто як 'це про мене' момент."""
    },
    {
        "type": "question_engagement",
        "prompt": """Напиши одне просте питання для людей які мають або планують бізнес.
Про сайт, про довіру клієнтів, про те як вони шукають послуги в інтернеті.
Питання має бути таке що хочеться відповісти в коментарі."""
    },
    {
        "type": "developer_pain",
        "prompt": """Напиши пост про типову ситуацію яка стається між клієнтом і розробником.
Розробник кинув проект, тягнув місяцями, зробив не те. Від сторони людини яка це бачила.
Без злості, просто як факт. Коротко."""
    },
    {
        "type": "ai_dev",
        "prompt": """Напиши пост про те як AI змінює розробку сайтів і що це означає для клієнтів.
Не технічно, а людською мовою. Типу 'раніше на це йшов місяць, тепер тиждень'.
Без хайпу, без 'революція'. Просто факт з роботи."""
    },
    {
        "type": "value_tip",
        "prompt": """Напиши один конкретний тіп для власника малого бізнесу.
Про сайт або онлайн присутність. Щось що можна перевірити або зробити прямо зараз.
Коротко. Без вступу типу 'сьогодні розкажу'."""
    }
]

SYSTEM_PROMPT = """Ти пишеш пости для Threads від імені Богдана Ходакова (@hodakov.digital).
Богдан розробляє сайти і додатки з AI. Робить за 5-7 днів від дизайну до запуску.

ЖОРСТКО ЗАБОРОНЕНО:
- Тире як пунктуація
- Слова: критично, важливо, ключовий, унікальний, рішення, підхід, результат, онлайн-присутність, цифровий простір
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
Я потім ще довго думав, може міг назвати іншу. Але ні, ціна була нормальна.
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


def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_log(posts):
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)


def generate_post():
    pillar = random.choice(CONTENT_PILLARS)

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=400,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": pillar["prompt"]}
        ]
    )

    text = response.choices[0].message.content.strip()
    return text, pillar["type"]


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
        time.sleep(30)  # Threads вимагає паузу перед публікацією
        post_id = publish_threads_post(creation_id)

        log_entry = {
            "timestamp": timestamp,
            "type": pillar_type,
            "text": text,
            "post_id": post_id,
            "status": "published"
        }

        posts = load_log()
        posts.append(log_entry)
        save_log(posts)

        print(f"Опубліковано! ID: {post_id}")

    except Exception as e:
        print(f"Помилка: {e}")
        log_entry = {
            "timestamp": timestamp,
            "status": "error",
            "error": str(e)
        }
        posts = load_log()
        posts.append(log_entry)
        save_log(posts)


# ===== РОЗКЛАД: 3 пости на день (UTC+3 Київ) =====
# Railway сервер на UTC, тому віднімаємо 3 години
schedule.every().day.at("06:00").do(post_to_threads)  # 09:00 Київ
schedule.every().day.at("10:30").do(post_to_threads)  # 13:30 Київ
schedule.every().day.at("16:00").do(post_to_threads)  # 19:00 Київ

if __name__ == "__main__":
    print("Threads AutoPoster — hodakov.digital")
    print("Розклад: 09:00 / 13:30 / 19:00")
    print("Перший пост зараз...")
    post_to_threads()

    while True:
        schedule.run_pending()
        time.sleep(60)