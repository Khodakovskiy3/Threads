"""
Спільний шар збереження стану для main.py (worker) і dashboard.py (web).

Раніше все зберігалось в json-файлах на диску (проблема: Railway без Volume обнуляє
файлову систему при кожному деплої). Тепер, якщо підключений DATABASE_URL (Postgres на
Railway), все йде туди — переживає деплої, і обидва сервіси (worker і dashboard) бачать
той самий актуальний стан без ризику що хтось перезаписав файл одночасно з іншим процесом.

Якщо DATABASE_URL не заданий (напр. локальний запуск для тестів) — тихо падає назад
на json-файли в DATA_DIR, як було раніше. Це запасний варіант, не основний шлях для продакшену.
"""

import os
import json

DATABASE_URL = os.getenv("DATABASE_URL")

DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

_conn = None

if DATABASE_URL:
    import psycopg2
    from psycopg2.extras import Json


def _file_path(key):
    return os.path.join(DATA_DIR, f"{key}.json")


def _load_json_file(key, default):
    path = _file_path(key)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def _save_json_file(key, data):
    with open(_file_path(key), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_connection():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(DATABASE_URL)
        _conn.autocommit = True
    return _conn


def init_db():
    """Створює таблицю kv_store якщо її ще нема. Викликати один раз при старті
    і worker'а (main.py), і dashboard.py — обидва можуть це зробити безпечно (IF NOT EXISTS)."""
    if not DATABASE_URL:
        print("DATABASE_URL не заданий — використовую json-файли в DATA_DIR як тимчасовий варіант")
        return
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
        print("Підключено до Postgres (DATABASE_URL), таблиця kv_store готова")
    except Exception as e:
        print(f"Не вдалось підключитись до Postgres: {e}. Падаю назад на json-файли")


def load_json(key, default):
    if not DATABASE_URL:
        return _load_json_file(key, default)
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row is not None else default
    except Exception as e:
        print(f"Помилка читання з Postgres [{key}]: {e}. Падаю назад на json-файл")
        return _load_json_file(key, default)


def save_json(key, data):
    if not DATABASE_URL:
        _save_json_file(key, data)
        return
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO kv_store (key, value, updated_at) VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """, (key, Json(data)))
    except Exception as e:
        print(f"Помилка запису в Postgres [{key}]: {e}. Пишу в json-файл як запасний варіант")
        _save_json_file(key, data)
