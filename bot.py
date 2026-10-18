"""
Бот: берёт новые посты из t.me/s/tengri_kz, добавляет словарик
(казахский → русский, средний уровень) через Google Gemini (бесплатный тариф)
и присылает в личку.

Запуск один раз (для GitHub Actions):  python bot.py
Запуск в цикле на своём ПК:            python bot.py --loop
"""

import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CHANNEL = os.getenv("CHANNEL", "tengri_kz")
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
# первая модель основная; если у неё кончился бесплатный лимит — пробуем следующую
GEMINI_MODELS = [m.strip() for m in
                 os.getenv("GEMINI_MODELS", "gemini-3.8-flash,gemini-3.5-flash-lite").split(",")
                 if m.strip()]
STATE_FILE = Path(os.getenv("STATE_FILE", Path(__file__).with_name("last_id.txt")))
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "15"))
LOOP_INTERVAL_SEC = int(os.getenv("LOOP_INTERVAL_SEC", "600"))

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

PROMPT = """Ты помогаешь русскоязычному человеку учить казахский язык (средний уровень, B1–B2).
Ниже новость на казахском. Выбери из неё 5–10 слов и устойчивых выражений, которые полезны
для среднего уровня: не самые базовые (типа «адам», «бүгін», «қала»), но и не имена собственные,
не цифры и не слова, одинаковые с русскими (полиция, депортация и т.п.).
Предпочитай устойчивые сочетания и глагольные конструкции (например «қамауға алу»).
Пиши слово в той форме, как оно в тексте; если форма сильно отличается от словарной,
в поле "base" укажи словарную форму, иначе оставь "base" пустым.
Перевод — коротко, по смыслу в этом тексте.

Ответь ТОЛЬКО JSON-массивом, без пояснений:
[{"kz": "...", "base": "...", "ru": "..."}]

Новость:
"""


# ---------- парсинг канала ----------

def fetch_posts():
    """Возвращает список (id, text) с публичной страницы канала, по возрастанию id."""
    r = requests.get(f"https://t.me/s/{CHANNEL}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        try:
            post_id = int(msg["data-post"].split("/")[-1])
        except ValueError:
            continue
        # цитата из поста, на который отвечают, нам не нужна
        for reply in msg.select(".tgme_widget_message_reply"):
            reply.decompose()
        text_div = msg.select_one("div.tgme_widget_message_text")
        if not text_div:
            continue  # пост без текста (только фото/видео) — пропускаем
        for br in text_div.find_all("br"):
            br.replace_with("\n")
        text = text_div.get_text().strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        if text:
            posts.append((post_id, text))
    posts.sort()
    return posts


# ---------- словарик ----------

def ask_gemini(prompt):
    last_error = None
    for model in GEMINI_MODELS:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
            },
            timeout=60,
        )
        if r.status_code in (429, 404, 503):  # лимит, модель недоступна, перегрузка
            last_error = f"{model}: {r.status_code} {r.text[:200]}"
            print(f"Gemini {last_error} — пробую следующую модель")
            continue
        if not r.ok:
            raise RuntimeError(f"Gemini {model}: {r.status_code} {r.text[:300]}")
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))
    raise RuntimeError(f"Все модели Gemini недоступны. Последняя ошибка: {last_error}")


def make_glossary(text):
    raw = ask_gemini(PROMPT + text).strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    start, end = raw.find("["), raw.rfind("]")
    items = json.loads(raw[start:end + 1])
    return [i for i in items if i.get("kz") and i.get("ru")]


# ---------- отправка ----------

def build_message(post_id, text, glossary):
    url = f"https://t.me/{CHANNEL}/{post_id}"
    lines = []
    for item in glossary:
        kz = html.escape(item["kz"])
        base = item.get("base", "").strip()
        base_part = f" <i>({html.escape(base)})</i>" if base and base.lower() != item["kz"].lower() else ""
        lines.append(f"• <b>{kz}</b>{base_part} — {html.escape(item['ru'])}")
    footer = "\n\n📚 <b>Словарик:</b>\n" + "\n".join(lines) if lines else \
             "\n\n📚 <i>Словарик не получился, попробуйте позже.</i>"
    link = f'\n\n<a href="{url}">Оригинал</a>'

    # лимит Telegram — 4096 символов; при необходимости режем текст новости
    budget = 4000 - len(footer) - len(link)
    body = html.escape(text)
    if len(body) > budget:
        body = body[:budget].rsplit(" ", 1)[0] + "…"
    return body + footer + link, url


def send(message, url):
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            # превью ссылки на пост — так видно фото из новости
            "link_preview_options": {"url": url, "prefer_small_media": True},
        },
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram: {r.status_code} {r.text}")


# ---------- состояние ----------

def load_last_id():
    try:
        return int(STATE_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def save_last_id(post_id):
    STATE_FILE.write_text(str(post_id))


# ---------- основной цикл ----------

def run_once():
    posts = fetch_posts()
    if not posts:
        print("Постов не найдено")
        return

    last_id = load_last_id()
    if last_id is None:
        # первый запуск: присылаем только самый свежий пост, чтобы не завалить старыми
        new_posts = posts[-1:]
    else:
        new_posts = [p for p in posts if p[0] > last_id][-MAX_POSTS_PER_RUN:]

    print(f"Новых постов: {len(new_posts)}")
    for post_id, text in new_posts:
        try:
            glossary = make_glossary(text)
        except Exception as e:  # новость всё равно отправим, без словарика
            print(f"Ошибка словарика для {post_id}: {e}")
            glossary = []
        message, url = build_message(post_id, text, glossary)
        send(message, url)
        save_last_id(post_id)
        print(f"Отправлен пост {post_id}")
        time.sleep(13)  # бесплатный тариф Gemini ограничивает число запросов в минуту


if __name__ == "__main__":
    if "--loop" in sys.argv:
        while True:
            try:
                run_once()
            except Exception as e:
                print(f"Ошибка: {e}")
            time.sleep(LOOP_INTERVAL_SEC)
    else:
        run_once()
