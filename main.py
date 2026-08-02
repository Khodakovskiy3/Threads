from openai import OpenAI
import requests
import schedule
import time
import random
import os
import json
import hashlib
import threading
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
POSTING_JITTER_FILE = "posting_jitter"  # рандомізований час наступної публікації
TRENDS_FILE = "content_trends"  # щоденний аналіз трендів у ніші

# Постійна клавіатура внизу чату — щоб не пам'ятати команди напам'ять.
# Сайт (dashboard.py) більше не відкривається через бота — це окремий постійний сайт,
# налаштування сповіщень тепер теж там, а не тут. Бот лишається тільки для сповіщень і швидких команд.
MAIN_KEYBOARD = {
    "keyboard": [
        ["Аналіз", "Нові ідеї"],
        ["Допомога"]
    ],
    "resize_keyboard": True
}

BOT_COMMANDS = [
    {"command": "analysis", "description": "Останній аналіз постів текстом"},
    {"command": "ideas", "description": "Згенерувати нові ідеї для постів"},
    {"command": "help", "description": "Що вміє бот"},
]

# Типи постів які вважаються "експертними" — саме вони дублюються
# розширеною версією в Telegram і отримують CTA в кінці Threads-поста.
# client_risk_check навмисно виключений — це радше рефлексія, а не лайфхак, туди CTA виглядає недоречно.
EXPERT_TYPES = {"value_tip", "ai_dev"}

TELEGRAM_CTA = "\n\nРозписую детальніше в Telegram: t.me/hodakov_digital"

# ===== ПОШУК ЛІДІВ І САМОРЕКЛАМИ (обхід через Google, поки нема App Review Meta) =====
GOOGLE_SEARCH_API_KEY = os.getenv("GOOGLE_SEARCH_API_KEY")
GOOGLE_SEARCH_CX = os.getenv("GOOGLE_SEARCH_CX")
TELEGRAM_USER_CHAT_ID = os.getenv("TELEGRAM_USER_CHAT_ID")  # особистий чат з ботом (не канал)

SEEN_LEADS_FILE = "seen_leads"
SEEN_SELFPROMO_FILE = "seen_selfpromo"
PENDING_REPLIES_FILE = "pending_replies"
TELEGRAM_OFFSET_FILE = "telegram_offset"
REPLY_TEMPLATE_STATE_FILE = "reply_template_state"
REAL_CONTEXT_FILE = "real_context"  # реальні ситуації від Романа для storytelling_client (через "контекст: ...")
NOTIFICATION_SETTINGS_FILE = "notification_settings"  # налаштовується на сайті (dashboard.py), не в боті

DEFAULT_NOTIFICATION_SETTINGS = {
    "post_stats": True,   # повідомлення "допис набрав стільки перегладів..."
    "leads": True,        # сповіщення про потенційних клієнтів
    "selfpromo": True,    # сповіщення про саморекламу конкурентів
}


def notifications_enabled(key):
    """Читає налаштування сповіщень зі сховища — керується з сайту, не з бота."""
    settings = load_json(NOTIFICATION_SETTINGS_FILE, DEFAULT_NOTIFICATION_SETTINGS)
    return settings.get(key, True)

# ===== САМОНАВЧАННЯ: пороги й параметри пауз =====
MIN_POSTS_FOR_ANALYSIS = 12       # мінімум постів з метриками (старших ANALYSIS_MIN_AGE_HOURS) для першого аналізу
ANALYSIS_MIN_AGE_HOURS = 24       # враховуємо в аналізі тільки пости старші за це, щоб дати метрикам "дозріти"
ANGLE_PAUSE_DAYS = 14             # на скільки днів ставиться на паузу конкретний кут (angle_id) після hard zero
TOPIC_PAUSE_DAYS = 21             # на скільки днів ставиться на паузу вся тема (topic_id)
HARD_ZERO_RATIO = 0.2             # поріг: скор <= медіана*це І нуль лайків/відповідей/репостів/цитат = hard zero
ABSOLUTE_LOW_VIEWS = 30            # незалежно від медіани: перегляди нижче цього і 0 взаємодій = теж hard zero
                                    # (захист від ситуації коли майже ВСІ пости слабкі — медіана сама занижена
                                    # і відносний поріг нічого не ловить)
TOPIC_FAIL_THRESHOLD = 2          # скільки різних кутів однієї теми мають "провалитись", щоб паузити всю тему

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
Щось що змусить власника впізнати свою ситуацію — це може бути будь-яка ніша малого бізнесу,
не обов'язково кав'ярня чи салон краси (дивись список ніш нижче в цьому промті, якщо є).
Без реклами. Просто як 'це про мене' момент."""
    },
    {
        "type": "question_engagement",
        "prompt": """Напиши одне просте питання для людей які мають або планують бізнес.
Про сайт, про довіру клієнтів, про те як вони знаходять послуги в інтернеті.
Питання має бути таке що хочеться відповісти в коментарі."""
    },
    {
        "type": "client_risk_check",
        "prompt": """Напиши пост про те, як клієнт може перевірити розробника ще ДО того як платити —
одне конкретне питання, деталь чи ознака яка одразу показує чи варто довіряти.
Подай це через власний досвід чи конкретний випадок, не як список порад і не як нотацію
"остерігайтесь поганих розробників". Тон спокійний, по суті, без злості на когось конкретного.
Ідея в тому щоб показати що тобі самому нема чого приховувати від такої перевірки."""
    },
    {
        "type": "ai_dev",
        "prompt": """Напиши конкретний лайфхак з власної розробки на основі AI-інструментів
(Claude, Cursor, ChatGPT). Це має бути прийом який можна застосувати прямо зараз:
як економити токени, який промт реально працює краще, який інструмент рятує час,
як побудувати процес щоб AI менше плутався. Без загальних роздумів про 'майбутнє AI'
чи 'як AI змінює індустрію'. Один конкретний прийом, по суті, як людина ділиться
знахідкою з роботи.
Перше речення має бути хуком (пряме звернення, контрінтуїтивна заява чи конкретна
деталь) — не 'сьогодні розкажу про' чи загальний вступ."""
    },
    {
        "type": "value_tip",
        "prompt": """Напиши один конкретний тіп для власника малого бізнесу про сайт.
Щось що можна перевірити або зробити прямо зараз.
Дай пораду через особистий досвід чи конкретний випадок, а не через абстрактну статистику
типу 'X% користувачів роблять Y'. Коротко. Без вступу типу 'сьогодні розкажу'.
Перше речення має бути хуком — питання в лоб чи пряме звернення до власника бізнесу,
а не поступовий розгін до суті."""
    }
]

# Пул ніш малого бізнесу для прикладів в постах. (label, стем-слово для пошуку в тексті останніх постів,
# щоб не повторювати ту саму нішу — кав'ярня явно передомінувала раніше і пости стали одноманітними).
AUDIENCE_NICHES = [
    ("кав'ярня", "кав'яр"),
    ("перукарня чи барбершоп", "перукар"),
    ("салон краси чи манікюр", "манікюр"),
    ("автосервіс", "автосерв"),
    ("стоматологія", "стоматол"),
    ("ветклініка", "ветклін"),
    ("фітнес-студія чи тренажерний зал", "фітнес"),
    ("репетитор чи мовна школа", "репетитор"),
    ("флористика чи квітковий магазин", "флорист"),
    ("кондитерська чи пекарня", "кондитер"),
    ("магазин одягу", "магазин одяг"),
    ("майстерня з ремонту техніки", "ремонт техн"),
    ("юридичні чи бухгалтерські послуги", "юрист"),
    ("будівельна бригада чи ремонт квартир", "будівель"),
    ("фотограф", "фотограф"),
    ("автошкола", "автошкол"),
    ("організація свят чи event-агенція", "агенці"),
    ("клінінгова служба", "клінінг"),
    ("шиномонтаж", "шиномонт"),
    ("масажний кабінет", "масаж"),
]


def recent_used_niches(n=6):
    """Дивиться на останні N опублікованих постів і повертає ніші які там вже згадувались —
    щоб не писати знову і знову про кав'ярню чи будь-яку одну нішу поспіль."""
    posts = [p for p in load_log() if p.get("status") == "published" and p.get("text")]
    recent_text = " ".join(p["text"].lower() for p in posts[-n:])
    return [label for label, stem in AUDIENCE_NICHES if stem in recent_text]


def diversity_instruction():
    """Будує інструкцію яка забороняє повторювати нещодавно використані ніші і підказує нові."""
    used = recent_used_niches()
    unused = [label for label, _ in AUDIENCE_NICHES if label not in used]
    pool = unused if unused else [label for label, _ in AUDIENCE_NICHES]
    suggestions = random.sample(pool, min(4, len(pool)))

    if used:
        return (f"\n\nОстанні пости вже були про: {', '.join(used)}. У ЦЬОМУ пості візьми ІНШУ нішу малого "
                f"бізнесу, не ту саму — наприклад: {', '.join(suggestions)}. Якщо в пості взагалі не потрібен "
                f"конкретний приклад бізнесу, просто пропусти цю вказівку.")
    return (f"\n\nЯкщо в пості потрібен приклад ніші малого бізнесу — не бери завжди кав'ярню, спробуй щось "
            f"з цього: {', '.join(suggestions)}.")


BASE_SYSTEM_PROMPT = """Ти пишеш пости для Threads від імені Романа (@hodakov.digital).
Роман розробляє сайти і додатки з AI за 5-7 днів від дизайну до запуску.

ЖОРСТКО ЗАБОРОНЕНО:
- Тире як пунктуація
- Слова: критично, важливо, ключовий, унікальний, рішення, підхід, результат, онлайн-присутність
- Списки
- Хештеги і заклики підписатись
- Повчальні висновки типу "це важливо для бізнесу"
- "Не X, а Y" конструкції
- Емодзі

ПЕРШИЙ РЯДОК — ЦЕ ХУК:
Людина вирішує читати далі чи гортати далі за перші 1-2 секунди. Тому перше речення
має зупиняти скрол, а не бути розгоном чи вступом. Робочі формули хука:
- Питання-шпилька в лоб: "Скільки клієнтів твій сайт вже втратив?"
- Пряме звернення до конкретної аудиторії: "Якщо в тебе кав'ярня і досі нема сайту..."
- Контрінтуїтивна заява: "Тобі не потрібен дизайнер."
- Cold open в середину діалогу чи ситуації, без пояснення хто і що (контекст добудовується по ходу)
- Конкретна деталь замість загальної: не "клієнт написав дивне повідомлення", а "клієнт написав о 23:41"
НЕ починай з розгону, дати, чи "сьогодні хочу розповісти" — перше речення саме і є найсильніша частина поста.

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
    return load_json(INSIGHTS_FILE, {
        "insights": None,
        "updated_at": None,
        "based_on_posts": 0,
        "winning_patterns": [],
        "avoid_patterns": [],
        "boost_topics": [],
        "paused_topics": [],    # [{"topic_id": "...", "until": "дд.мм.рррр гг:хх"}]
        "paused_angles": [],    # [{"angle_id": "...", "topic_id": "...", "until": "дд.мм.рррр гг:хх"}]
        "topic_fail_counts": {},   # {topic_id: к-ть різних кутів що провалились}
        "failed_angle_ids": []    # щоб один і той самий кут не рахувався двічі
    })


def save_insights(data):
    save_json(INSIGHTS_FILE, data)


def load_real_context():
    return load_json(REAL_CONTEXT_FILE, [])


def save_real_context(items):
    save_json(REAL_CONTEXT_FILE, items)


# ===== САМОНАВЧАННЯ: допоміжні функції =====
def engagement_score(metrics):
    """Перегляди лишаються основним сигналом — акаунт росте, охоплення зараз важливіше за все.
    Лайки/відповіді/репости/цитати додаються зверху як бонус (репост і цитата важать більше,
    бо це людина сама поширила пост, а не просто побачила)."""
    if not metrics:
        return 0
    return (
        metrics.get("views", 0)
        + metrics.get("likes", 0)
        + metrics.get("replies", 0) * 2
        + metrics.get("reposts", 0) * 3
        + metrics.get("quotes", 0) * 3
    )


def post_age_hours(post):
    try:
        post_time = datetime.strptime(post["timestamp"], "%d.%m.%Y %H:%M")
    except Exception:
        return None
    return (datetime.now() - post_time).total_seconds() / 3600


def is_paused(topic_id, angle_id, state):
    """Деталізована, обчислена кодом (не GPT) перевірка чи ця тема/кут зараз на паузі."""
    now = datetime.now()
    for p in state.get("paused_topics", []):
        if p.get("topic_id") == topic_id:
            try:
                if now < datetime.strptime(p["until"], "%d.%m.%Y %H:%M"):
                    return True, f"тема '{topic_id}' на паузі до {p['until']}"
            except Exception:
                continue
    for a in state.get("paused_angles", []):
        if a.get("angle_id") == angle_id:
            try:
                if now < datetime.strptime(a["until"], "%d.%m.%Y %H:%M"):
                    return True, f"кут '{angle_id}' на паузі до {a['until']}"
            except Exception:
                continue
    return False, None


def active_pause_summary(state):
    now = datetime.now()
    lines = []
    for p in state.get("paused_topics", []):
        try:
            if now < datetime.strptime(p["until"], "%d.%m.%Y %H:%M"):
                lines.append(f"тема '{p['topic_id']}' (до {p['until']})")
        except Exception:
            continue
    for a in state.get("paused_angles", []):
        try:
            if now < datetime.strptime(a["until"], "%d.%m.%Y %H:%M"):
                lines.append(f"кут '{a['angle_id']}' (до {a['until']})")
        except Exception:
            continue
    return lines


# ===== МЕТРИКИ =====
def fetch_post_metrics(post_id):
    """Отримує перегляди, лайки, репости для поста"""
    url = f"https://graph.threads.net/v1.0/{post_id}/insights"
    params = {
        "metric": "views,likes,replies,reposts,quotes",
        "access_token": THREADS_ACCESS_TOKEN
    }
    response = requests.get(url, params=params, timeout=15)
    data = response.json()

    metrics = {}
    for item in data.get("data", []):
        val = item.get("values", [{}])[0].get("value", 0)
        metrics[item["name"]] = val
    return metrics


def notify_post_metrics(post, metrics):
    """Разове інформаційне повідомлення власнику коли для поста вперше підтягнулись метрики.
    Вмикається/вимикається на сайті (вкладка Сповіщення), а не в самому боті."""
    if not notifications_enabled("post_stats"):
        return
    preview = (post.get("text") or "").replace("\n", " ").strip()[:80]
    message = (
        f"Статистика допису ({post.get('type', '?')}):\n"
        f"\"{preview}{'...' if len(post.get('text') or '') > 80 else ''}\"\n\n"
        f"Перегляди: {metrics.get('views', 0)}\n"
        f"Лайки: {metrics.get('likes', 0)}\n"
        f"Відповіді: {metrics.get('replies', 0)}\n"
        f"Репости: {metrics.get('reposts', 0)}\n"
        f"Цитати: {metrics.get('quotes', 0)}"
    )
    send_telegram_dm(message)


def update_metrics():
    """Підтягує метрики для постів старших 3 годин (Threads API не одразу віддає реальні перегляди),
    і одразу шле власнику інформаційне повідомлення по кожному такому посту (одноразово)."""
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
                notify_post_metrics(post, metrics)
        except Exception as e:
            print(f"Помилка метрик: {e}")

    if updated:
        save_log(posts)


# ===== АНАЛІЗ І НАВЧАННЯ =====
def analyze_and_learn():
    """Аналізує які пости працюють краще, деталізовано (в коді, не GPT) паузить теми/кути
    які реально провалились, і оновлює структуровані рекомендації для генерації нових постів."""
    posts = load_log()
    eligible = []
    for p in posts:
        if p.get("status") != "published" or not p.get("metrics") or not p.get("text"):
            continue
        age = post_age_hours(p)
        if age is None or age < ANALYSIS_MIN_AGE_HOURS:
            continue
        eligible.append(p)

    if len(eligible) < MIN_POSTS_FOR_ANALYSIS:
        print(f"Недостатньо даних для аналізу ({len(eligible)}/{MIN_POSTS_FOR_ANALYSIS} "
              f"постів старших {ANALYSIS_MIN_AGE_HOURS}г)")
        return

    for p in eligible:
        p["_score"] = engagement_score(p["metrics"])

    sorted_posts = sorted(eligible, key=lambda x: x["_score"], reverse=True)
    sample_size = max(1, min(5, len(sorted_posts) // 2))
    top = sorted_posts[:sample_size]
    bottom = sorted_posts[-sample_size:]

    scores_sorted = sorted(p["_score"] for p in eligible)
    n = len(scores_sorted)
    median_score = (scores_sorted[n // 2] if n % 2 == 1
                     else (scores_sorted[n // 2 - 1] + scores_sorted[n // 2]) / 2)
    median_score = max(median_score, 1)

    # --- деталізована hard-zero пауза, порахована кодом, а не GPT ---
    state = load_insights()
    paused_topics = state.get("paused_topics", [])
    paused_angles = state.get("paused_angles", [])
    topic_fail_counts = dict(state.get("topic_fail_counts", {}))
    failed_angle_ids = set(state.get("failed_angle_ids", []))

    def until_str(days):
        return (datetime.now() + timedelta(days=days)).strftime("%d.%m.%Y %H:%M")

    new_hard_zero = []
    total_hard_zero_count = 0
    for p in eligible:
        angle_id = p.get("angle_id")
        topic_id = p.get("topic_id")

        m = p.get("metrics", {})
        raw_interactions = m.get("likes", 0) + m.get("replies", 0) + m.get("reposts", 0) + m.get("quotes", 0)
        is_hard_zero = (
            (p["_score"] <= median_score * HARD_ZERO_RATIO or m.get("views", 0) <= ABSOLUTE_LOW_VIEWS)
            and raw_interactions == 0
        )
        if is_hard_zero:
            total_hard_zero_count += 1

        if not angle_id or angle_id == "unknown":
            continue

        if is_hard_zero and angle_id not in failed_angle_ids:
            new_hard_zero.append((topic_id, angle_id))
            failed_angle_ids.add(angle_id)

            if not any(a.get("angle_id") == angle_id for a in paused_angles):
                paused_angles.append({"angle_id": angle_id, "topic_id": topic_id, "until": until_str(ANGLE_PAUSE_DAYS)})

            if topic_id and topic_id != "unknown":
                topic_fail_counts[topic_id] = topic_fail_counts.get(topic_id, 0) + 1
                if (topic_fail_counts[topic_id] >= TOPIC_FAIL_THRESHOLD
                        and not any(t.get("topic_id") == topic_id for t in paused_topics)):
                    paused_topics.append({"topic_id": topic_id, "until": until_str(TOPIC_PAUSE_DAYS)})

    # якщо провалюється більшість постів — це не проблема однієї теми, а системна проблема формату
    systemic_failure = total_hard_zero_count / len(eligible) > 0.5

    now = datetime.now()

    def not_expired(item):
        try:
            return datetime.strptime(item["until"], "%d.%m.%Y %H:%M") > now
        except Exception:
            return False

    paused_topics = [t for t in paused_topics if not_expired(t)]
    paused_angles = [a for a in paused_angles if not_expired(a)]

    def format_post(p):
        m = p.get("metrics", {})
        return (
            f"Тип: {p.get('type', '?')} | Тема: {p.get('topic_id', '?')} | Кут: {p.get('angle_id', '?')} | "
            f"Скор: {p['_score']} (перегляди {m.get('views', 0)}, лайки {m.get('likes', 0)}, "
            f"відповіді {m.get('replies', 0)}, репости {m.get('reposts', 0)}, цитати {m.get('quotes', 0)})\n"
            f"Текст: {p['text']}"
        )

    systemic_note = ""
    if systemic_failure:
        systemic_note = (f"\n\nУВАГА: {total_hard_zero_count} з {len(eligible)} постів ({total_hard_zero_count*100//len(eligible)}%) "
                          f"взагалі не набрали перегляди і 0 взаємодій. Це НЕ проблема однієї теми — це системна "
                          f"проблема формату/хука/стилю. Врахуй це в avoid_patterns і writer_summary: треба радикальніше "
                          f"змінити підхід, не просто уникати конкретних тем.")

    prompt = f"""Проаналізуй пости Threads акаунту розробника сайтів @hodakov.digital.
Скор = перегляди + лайки + відповіді*2 + репости*3 + цитати*3. Охоплення (перегляди) — головний
сигнал, бо акаунт зараз росте і потрібна саме кількість людей що побачили пост; реакції додаються
зверху як бонус, а не замінюють перегляди.
{systemic_note}

ТОП ПОСТИ ЗА СКОРОМ:
{chr(10).join([format_post(p) for p in top])}

СЛАБКІ ПОСТИ ЗА СКОРОМ:
{chr(10).join([format_post(p) for p in bottom])}

Поверни відповідь СТРОГО у форматі JSON, без пояснень навколо:
{{
  "winning_patterns": ["коротке конкретне спостереження про те що працює", "..."],
  "avoid_patterns": ["коротке конкретне спостереження про те чого уникати", "..."],
  "boost_topics": ["тема чи topic_id який варто розвивати далі", "..."],
  "writer_summary": "3-5 речень зрозумілого тексту з конкретними висновками для того хто пише наступні пости"
}}
Тільки конкретні спостереження на основі цих даних, без загальних порад типу "публікуйте частіше"."""

    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=600,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}]
        )
        parsed = json.loads(response.choices[0].message.content.strip())
    except Exception as e:
        print(f"Не вдалось розпарсити GPT-аналіз, зберігаю тільки код-обчислені паузи: {e}")
        parsed = {}

    winning_patterns = parsed.get("winning_patterns") or state.get("winning_patterns", [])
    avoid_patterns = parsed.get("avoid_patterns") or state.get("avoid_patterns", [])
    boost_topics = parsed.get("boost_topics") or state.get("boost_topics", [])
    writer_summary = parsed.get("writer_summary") or state.get("insights")

    if systemic_failure:
        forced_note = (f"Більшість постів ({total_hard_zero_count}/{len(eligible)}) взагалі не набирають "
                        f"перегляди — потрібно суттєво міняти хук і формат, не тільки уникати окремих тем.")
        if forced_note not in avoid_patterns:
            avoid_patterns = [forced_note] + list(avoid_patterns)

    # Генеруємо покращені версії слабких постів
    post_improvements = state.get("post_improvements", "")
    if bottom and len(eligible) >= MIN_POSTS_FOR_ANALYSIS:
        try:
            weak_samples = "\n\n".join(format_post(p) for p in bottom[:3])
            imp_prompt = (
                "Ось пости що не набрали охоплення (@hodakov.digital):\n\n"
                + weak_samples
                + "\n\nДля кожного одним рядком:\n"
                "[Пост N] Проблема: <1 речення> | Новий хук: <перші 2-3 речення переробленого початку>"
            )
            imp_resp = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=350,
                messages=[{"role": "user", "content": imp_prompt}]
            )
            post_improvements = imp_resp.choices[0].message.content.strip()
        except Exception as e:
            print(f"Помилка генерації покращень постів: {e}")

    save_insights({
        "insights": writer_summary,
        "updated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "based_on_posts": len(eligible),
        "winning_patterns": winning_patterns,
        "avoid_patterns": avoid_patterns,
        "boost_topics": boost_topics,
        "paused_topics": paused_topics,
        "paused_angles": paused_angles,
        "topic_fail_counts": topic_fail_counts,
        "failed_angle_ids": list(failed_angle_ids),
        "post_improvements": post_improvements,
    })

    print(f"\nАналіз оновлено на основі {len(eligible)} постів (старших {ANALYSIS_MIN_AGE_HOURS}г).")
    if new_hard_zero:
        print(f"Нові кути на паузі (hard zero, 0 взаємодій): {new_hard_zero}")
    if writer_summary:
        print(writer_summary)


# ===== ГЕНЕРАЦІЯ ПОСТІВ =====
def get_next_allowed_post_time():
    """Повертає datetime коли дозволена наступна публікація (з урахуванням рандомного джиттера)."""
    data = load_json(POSTING_JITTER_FILE, {})
    if data.get("next_post_allowed"):
        try:
            return datetime.strptime(data["next_post_allowed"], "%d.%m.%Y %H:%M")
        except Exception:
            pass
    return None


def set_next_allowed_post_time(interval_hours):
    """Зберігає час наступної публікації з рандомним джиттером ±20 хвилин."""
    jitter_minutes = random.randint(-20, 25)
    actual_hours = max(0.5, interval_hours + jitter_minutes / 60)
    next_time = datetime.now() + timedelta(hours=actual_hours)
    save_json(POSTING_JITTER_FILE, {
        "next_post_allowed": next_time.strftime("%d.%m.%Y %H:%M"),
        "jitter_minutes": jitter_minutes,
        "base_interval_hours": interval_hours,
    })
    print(f"Наступна публікація о {next_time.strftime('%H:%M')} (джиттер {jitter_minutes:+d}хв)")
    return next_time


def refresh_trend_context():
    """Оновлює аналіз трендів у ніші раз на добу через GPT."""
    trends_data = load_json(TRENDS_FILE, {})
    if trends_data.get("updated_at"):
        try:
            last = datetime.strptime(trends_data["updated_at"], "%d.%m.%Y %H:%M")
            if (datetime.now() - last).total_seconds() < 22 * 3600:
                return trends_data.get("trends", "")
        except Exception:
            pass

    today = datetime.now().strftime("%B %Y")
    prompt = (
        f"Ти аналітик контент-трендів для українських авторів у Threads та Instagram.\n"
        f"Зараз {today}.\n\n"
        "Визнач 5 конкретних трендів у нішах малого бізнесу, сайтів, AI для розробників в Україні:\n"
        "1. Що зараз активно обговорюють власники малого бізнесу (болі, страхи, запити)\n"
        "2. Які теми про AI-інструменти набирають охоплення серед розробників\n"
        "3. Які формати постів (гумор, спостереження, питання) краще заходять у Threads зараз\n"
        "4. Які болі клієнтів, що шукають розробника сайту, найактуальніші\n\n"
        "Формат: рівно 5 рядків з конкретикою, без вступів та загальних слів."
    )

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        trends = response.choices[0].message.content.strip()
        save_json(TRENDS_FILE, {
            "trends": trends,
            "updated_at": datetime.now().strftime("%d.%m.%Y %H:%M")
        })
        print(f"Тренди оновлено:\n{trends}")
        return trends
    except Exception as e:
        print(f"Помилка refresh_trend_context: {e}")
        return trends_data.get("trends", "")


def build_system_prompt():
    """Будує промпт з урахуванням накопичених структурованих інсайтів і активних пауз."""
    prompt = BASE_SYSTEM_PROMPT
    insights_data = load_insights()
    extra_parts = []

    if insights_data.get("insights"):
        extra_parts.append(
            f"АНАЛІЗ ПОПЕРЕДНІХ ПОСТІВ (що реально працює для цього акаунту):\n{insights_data['insights']}"
        )
    if insights_data.get("winning_patterns"):
        extra_parts.append(
            "ЩО ТОЧНО ПРАЦЮЄ (з реальних даних):\n"
            + "\n".join(f"- {w}" for w in insights_data["winning_patterns"])
        )
    if insights_data.get("avoid_patterns"):
        extra_parts.append(
            "ЧОГО УНИКАТИ (з реальних даних):\n"
            + "\n".join(f"- {a}" for a in insights_data["avoid_patterns"])
        )
    if insights_data.get("boost_topics"):
        extra_parts.append(
            "ТЕМИ ЯКІ ВАРТО РОЗВИВАТИ ДАЛІ:\n"
            + "\n".join(f"- {t}" for t in insights_data["boost_topics"])
        )

    paused = active_pause_summary(insights_data)
    if paused:
        extra_parts.append(
            "НЕ ПИШИ ЗАРАЗ ПРО ЦІ ТЕМИ/КУТИ (вони на паузі — реально погано заходили):\n"
            + "\n".join(f"- {p}" for p in paused)
        )

    # Тренди у ніші (оновлюються щодня)
    trends = load_json(TRENDS_FILE, {}).get("trends", "")
    if trends:
        extra_parts.append(
            "АКТУАЛЬНІ ТРЕНДИ У НІШІ (враховуй при виборі теми та формату):\n" + trends
        )

    if extra_parts:
        prompt += "\n\n" + "\n\n".join(extra_parts) + "\n\nВраховуй ці спостереження при написанні нового поста."

    return prompt


STRUCTURED_OUTPUT_INSTRUCTIONS = """
Поверни відповідь СТРОГО у форматі JSON (без markdown, без пояснень навколо), з полями:
{
  "post_text": "готовий текст поста, саме той що піде в публікацію",
  "topic_id": "короткий slug теми латиницею, напр. online_booking чи ai_token_saving",
  "angle_id": "короткий slug конкретного кута/ситуації всередині теми, напр. bookings_lost_in_direct",
  "hook_type": "question / direct_address / contrarian / cold_open / specific_detail",
  "audience": "коротко хто цільова аудиторія цього поста"
}
topic_id і angle_id придумай сам виходячи з того, про що реально цей пост — вони потрібні лише для
внутрішньої аналітики і ніколи не з'являються в самому пості."""


def pick_pillar_and_prompt():
    """Обирає пиллар і будує промпт-завдання. Для storytelling_client підставляє реальну
    ситуацію від Романа (якщо додана через "контекст: ..." в боті) — щоб не вигадувати клієнтів."""
    pillar = random.choice(CONTENT_PILLARS)
    pillar_prompt = pillar["prompt"]

    if pillar["type"] != "ai_dev":
        pillar_prompt += diversity_instruction()

    if pillar["type"] == "storytelling_client":
        context_list = load_real_context()
        if context_list:
            note = context_list.pop(0)
            save_real_context(context_list)
            pillar_prompt += f"\n\nРеальна ситуація для цього поста (використай саме її, нічого не вигадуй додатково): {note}"
        else:
            pillar_prompt += (
                "\n\nЗараз немає нової реальної ситуації від Романа. НЕ вигадуй конкретного діалогу "
                "чи цитати клієнта яких насправді не було. Замість цього напиши загальне спостереження "
                "чи роздум від першої особи про типову робочу ситуацію, без вигаданого конкретного "
                "клієнта чи вигаданої репліки."
            )

    return pillar, pillar_prompt


def generate_post():
    """Генерує пост зі структурованими метаданими (тема/кут/тип хука/аудиторія).
    Якщо GPT видав тему чи кут які зараз на паузі (реально погано заходили) — перегенеровує,
    до 3 спроб. Пауза перевіряється кодом (deterministic), не залишається на розсуд GPT."""
    system_prompt = build_system_prompt() + "\n\n" + STRUCTURED_OUTPUT_INSTRUCTIONS
    state = load_insights()
    client = OpenAI(api_key=OPENAI_API_KEY)

    last_result = None
    for attempt in range(3):
        pillar, pillar_prompt = pick_pillar_and_prompt()
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=500,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": pillar_prompt}
                ]
            )
            parsed = json.loads(response.choices[0].message.content.strip())
        except Exception as e:
            print(f"Не вдалось розпарсити структурований пост (спроба {attempt + 1}): {e}")
            continue

        post_text = (parsed.get("post_text") or "").strip()
        if not post_text:
            continue

        topic_id = parsed.get("topic_id", "unknown")
        angle_id = parsed.get("angle_id", "unknown")
        hook_type = parsed.get("hook_type", "unknown")
        audience = parsed.get("audience", "unknown")

        last_result = (post_text, pillar["type"], topic_id, angle_id, hook_type, audience)

        paused, reason = is_paused(topic_id, angle_id, state)
        if not paused:
            return last_result

        print(f"Тема/кут з паузи ({reason}) — перегенеровую (спроба {attempt + 1})")

    if last_result:
        print("Не вдалось уникнути паузованої теми за 3 спроби — публікую останню згенеровану версію")
        return last_result

    # Крайній фолбек без структурованого виводу, щоб публікація не зламалась зовсім
    pillar, pillar_prompt = pick_pillar_and_prompt()
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=400,
        messages=[
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": pillar_prompt}
        ]
    )
    text = response.choices[0].message.content.strip()
    return text, pillar["type"], "unknown", "unknown", "unknown", "unknown"


# ===== TELEGRAM (кросспост експертних постів) =====
def generate_telegram_post(threads_text, pillar_type):
    """Розширює короткий експертний Threads-пост у повноцінний Telegram-пост."""
    system_prompt = """Ти пишеш пост для Telegram каналу Романа (@hodakov_digital),
розробника сайтів і додатків. Це той самий канал куди Роман веде людей з Threads.

Тобі дають короткий пост з Threads. Розпиши цю саму думку ширше і конкретніше:
додай реальний приклад, конкретну деталь або крок, який людина може забрати собі.
Це має відчуватись як продовження думки, а не переказ того самого поста.

ПЕРШИЙ РЯДОК — ЦЕ ХУК, так само як в Threads-пості: конкретна деталь, пряме звернення
чи контрінтуїтивна заява. Людина вирішує читати чи гортати далі за перше речення.
Не починай з розгону чи "продовжуючи попередню думку".

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
        response = requests.post(url, params=params, timeout=15)
        data = response.json()
        if not data.get("ok"):
            raise Exception(f"Помилка публікації в Telegram: {data}")
        message_id = data["result"]["message_id"]

        if not fits_caption:
            extra_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(extra_url, params={"chat_id": TELEGRAM_CHANNEL_ID, "text": text}, timeout=15)

        return message_id

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    params = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text
    }
    response = requests.post(url, params=params, timeout=15)
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
    response = requests.post(url, params=params, timeout=20)
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
    response = requests.post(url, params=params, timeout=20)
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

    # Перевірка рандомізованого часу наступної публікації
    next_allowed = get_next_allowed_post_time()
    if next_allowed and datetime.now() < next_allowed:
        print(f"\n[{timestamp}] Наступна публікація о {next_allowed.strftime('%H:%M')} — пропускаю")
        return

    # Fallback якщо jitter-файл порожній
    if next_allowed is None:
        hours_since = hours_since_last_post()
        if hours_since is not None and hours_since < interval:
            remaining = interval - hours_since
            set_next_allowed_post_time(remaining)
            print(f"\n[{timestamp}] Ставлю рандомний таймер")
            return

    print(f"\n[{timestamp}] Генерую пост...")

    try:
        text, pillar_type, topic_id, angle_id, hook_type, audience = generate_post()
        is_expert = pillar_type in EXPERT_TYPES
        published_text = text + TELEGRAM_CTA if is_expert else text

        print(f"Тип: {pillar_type} | тема: {topic_id} | кут: {angle_id} "
              f"{'(експертний → CTA + Telegram)' if is_expert else ''}")
        print(f"Текст:\n{'-'*40}\n{published_text}\n{'-'*40}")

        creation_id = create_threads_container(published_text)
        time.sleep(30)
        post_id = publish_threads_post(creation_id)

        posts = load_log()
        posts.append({
            "timestamp": timestamp,
            "type": pillar_type,
            "topic_id": topic_id,
            "angle_id": angle_id,
            "hook_type": hook_type,
            "audience": audience,
            "text": published_text,
            "post_id": post_id,
            "status": "published"
        })
        save_log(posts)
        print(f"Опубліковано! ID: {post_id}")

        # Ставимо рандомізований час наступної публікації
        set_next_allowed_post_time(interval)

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
                "таблицю на сайті), щоб відповісти конкретно. Додай пару відео на сайті або "
                "почекай поки набіжать перегляди на пости — і питай знову."
            )
            return

        def fmt_post(p):
            m = p.get("metrics", {})
            return (f"[{p.get('type', '?')} / тема: {p.get('topic_id', '?')}] скор {engagement_score(m)} "
                    f"(перегляди {m.get('views', 0)}, лайки {m.get('likes', 0)}, "
                    f"репости {m.get('reposts', 0)}): {p.get('text', '')[:200]}")

        def fmt_video(v):
            return (f"[{v.get('platform')}] перегляди {v.get('views', '-')}, лайки {v.get('likes', '-')}, "
                    f"коментарі {v.get('comments', '-')}: {v.get('url', '')}")

        sorted_posts = sorted(with_metrics, key=lambda p: engagement_score(p.get("metrics")), reverse=True)

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


RANDOM_ASSOCIATION_WORDS = [
    "космос", "кулінарія", "спорт", "музика", "мода", "тварини", "подорожі",
    "погода", "гроші", "історія", "медицина", "кіно", "садівництво", "спогади дитинства",
    "весілля", "автомобілі", "море", "гори", "школа", "сон"
]


def generate_content_ideas(count=10):
    """Генерує нові ідеї для контент-плану.

    Техніка: беремо нішу (сайти для малого бізнесу, клієнти, AI в розробці) і з'єднуємо
    з випадковим словом далеким за змістом — неочевидний зв'язок часто дає найкращі ідеї,
    ніж пряме "напиши пост про X".

    Верифікація: кожна ідея обов'язково звіряється з тим що вже реально показало результат
    (топові пости за переглядами + накопичений аналіз) — якщо зв'язок не резонує з перевіреним
    паттерном, GPT має підібрати інший, а не видавати ідею що ні на що не спирається."""
    insights = load_insights()
    posts = load_log()
    posts_with_metrics = [p for p in posts if p.get("metrics") and p.get("text")]
    top_posts = sorted(posts_with_metrics, key=lambda p: engagement_score(p["metrics"]), reverse=True)
    top_texts = [p["text"] for p in top_posts[:8]]

    context = ""
    if insights.get("insights"):
        context += f"Аналіз того що вже добре заходить (перевірений паттерн):\n{insights['insights']}\n\n"
    if insights.get("boost_topics"):
        context += "Теми які варто розвивати далі:\n" + "\n".join(f"- {t}" for t in insights["boost_topics"]) + "\n\n"
    if top_texts:
        context += "ТОП пости за реальними переглядами (це і є перевірка — нове має резонувати з цим):\n"
        context += "\n---\n".join(top_texts)
    if not context:
        context = ("Даних по метриках ще нема. Орієнтуйся на больову точку аудиторії: власники малого "
                    "бізнесу які бояться що розробник кине проект або тягнутиме місяцями.")

    random_words = random.sample(RANDOM_ASSOCIATION_WORDS, min(6, len(RANDOM_ASSOCIATION_WORDS)))

    prompt = f"""Ти генеруєш контент-план для Threads акаунту розробника сайтів @hodakov.digital
(робить сайти з AI за 5-7 днів).

ТЕХНІКА ГЕНЕРАЦІЇ: візьми нішу (сайти для малого бізнесу, клієнти, AI в розробці) і з'єднай
її з одним із випадкових слів нижче. Шукай неочевидний, не буквальний зв'язок — не "сайт схожий
на X", а метафору чи ситуацію яка через це слово розкриває нішу з несподіваного боку.

Випадкові слова для з'єднання: {', '.join(random_words)}

{context}

ВЕРИФІКАЦІЯ (обов'язково): кожна ідея має резонувати з тим що показано вище як перевірений
паттерн — той самий тип гумору, той самий больовий нерв, той самий формат що вже спрацював.
Якщо зв'язок з випадковим словом не резонує з перевіреним паттерном — підбери інший зв'язок,
не видавай ідею яка ні на що не спирається.

Згенеруй {count} ідей. Кожна ідея конкретна, готова одразу перетворитись в пост чи відео —
не загальна тема типу "пост про AI", а конкретна ситуація чи думка.

Формат відповіді — рівно {count} рядків, без нумерації, без пояснення техніки, кожен рядок це одна ідея."""

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


def build_help_text():
    return (
        "Що вміє бот:\n\n"
        "Пости в Threads публікуються самі по розкладу (06-12 раз на 2 год, 12-16 щогодини, "
        "16-21 раз на 2 год, 21-23 один пост, 23-06 тиша).\n\n"
        "Аналіз — останній аналіз того що добре заходить, текстом прямо в чат.\n"
        "Нові ідеї — згенерувати ще ідей для контент-плану.\n\n"
        "Просто питання текстом (без /) — бот відповість спираючись на реальну статистику акаунту.\n\n"
        "Напиши 'контекст: ...' з реальною ситуацією з роботи — вона піде в наступний пост-історію "
        "про клієнта замість вигаданої.\n\n"
        "Після кожного поста, коли підтягнуться перегляди/лайки (десь через 3+ год), бот сам "
        "пришле повідомлення зі статистикою цього конкретного допису — це і є основна функція бота, "
        "самі сповіщення налаштовуються (вмикаються/вимикаються) на сайті.\n\n"
        "Сповіщення про лідів і саморекламні тредси, а також запити на публікацію в Telegram-канал "
        "приходять самі, з кнопками підтвердження.\n\n"
        "Алгоритм написання постів сам аналізує статистику, паузить теми і кути які реально погано "
        "заходять (0 взаємодій), і підказує собі що краще писати далі."
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
        response = requests.post(url, data=payload, timeout=15)
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
        requests.post(url, params=params, timeout=10)
    except Exception:
        pass


def register_bot_commands():
    """Реєструє список команд в меню '/' Telegram — викликати один раз при старті."""
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    try:
        requests.post(url, json={"commands": BOT_COMMANDS}, timeout=10)
    except Exception as e:
        print(f"Не вдалось зареєструвати команди бота: {e}")


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
    response = requests.get(url, params=params, timeout=15)
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
    Тільки сповіщення в Telegram, без автовідповіді — відповідати треба самому і швидко.
    Вмикається/вимикається на сайті — якщо вимкнено, навіть не витрачаємо Google-квоту на пошук."""
    if not notifications_enabled("leads"):
        return
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
    ніякої автопублікації без дозволу власника.
    Вмикається/вимикається на сайті — якщо вимкнено, навіть не витрачаємо Google-квоту на пошук."""
    if not notifications_enabled("selfpromo"):
        return
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
                "Готово, бот на зв'язку. Знизу є кнопки для швидкого доступу, або просто питай текстом. "
                "Статистику і налаштування сповіщень дивись на сайті.",
                reply_markup=MAIN_KEYBOARD
            )
            continue

        # Аналіз — останній style_insights.json прямо текстом в чат
        if text in ("/analysis", "Аналіз"):
            insights = load_insights()
            if insights.get("insights"):
                parts = [f"Аналіз (оновлено {insights.get('updated_at', '?')}, "
                         f"на основі {insights.get('based_on_posts', '?')} постів):\n\n{insights['insights']}"]
                if insights.get("winning_patterns"):
                    parts.append("Що працює:\n" + "\n".join(f"- {w}" for w in insights["winning_patterns"]))
                if insights.get("avoid_patterns"):
                    parts.append("Чого уникати:\n" + "\n".join(f"- {a}" for a in insights["avoid_patterns"]))
                paused = active_pause_summary(insights)
                if paused:
                    parts.append("Зараз на паузі (погано заходили):\n" + "\n".join(f"- {p}" for p in paused))
                send_telegram_dm("\n\n".join(parts))
            else:
                send_telegram_dm(f"Аналізу ще нема — з'явиться після {MIN_POSTS_FOR_ANALYSIS}+ "
                                  f"опублікованих постів з метриками (старших {ANALYSIS_MIN_AGE_HOURS}г).")
            continue

        # "контекст: ..." — реальна ситуація для storytelling_client, щоб не вигадувати клієнтів
        if text.lower().startswith("контекст:"):
            note = text.split(":", 1)[1].strip()
            if note:
                items = load_real_context()
                items.append(note)
                save_real_context(items)
                send_telegram_dm("Записав. Використаю в наступному пості-історії про клієнта.")
            continue

        # Нові ідеї — генерує і одразу показує текстом (той самий контент-план що і на сайті)
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
            send_telegram_dm(build_help_text())
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
schedule.every(24).hours.do(refresh_trend_context)    # оновлення трендів у ніші

def run_dashboard_in_background():
    """Стартує Flask-панель (dashboard.py) в окремому потоці того самого процесу —
    так не треба окремого Railway-сервісу, панель живе на тому ж домені що і воркер."""
    try:
        from dashboard import app as dashboard_app
        port = int(os.getenv("PORT", 8080))
        dashboard_app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
    except Exception as e:
        print(f"Не вдалось запустити dashboard в фоні: {e}")


if __name__ == "__main__":
    print("Threads AutoPoster — hodakov.digital")
    print("Розклад по Києву: 06-12 раз на 2год, 12-16 щогодини, 16-21 раз на 2год, 21-23 один пост, 23-06 тиша")
    init_db()
    register_bot_commands()
    threading.Thread(target=run_dashboard_in_background, daemon=True).start()
    refresh_trend_context()  # Завантажуємо тренди при старті
    print("Перевірка при старті (пропускається якщо не в вікні або останній пост був недавно)...")
    post_to_threads()

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            # запобіжник: жодна окрема задача не повинна вбивати весь воркер
            alert_error("schedule.run_pending", e)
        time.sleep(60)