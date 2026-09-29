import os
import threading

import requests
from flask import Flask, request

TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER = int(os.environ["OWNER_CHAT_ID"])
SECRET = os.environ["WEBHOOK_SECRET"]
AI_KEY = os.environ["AI_API_KEY"]
AI_MODEL = os.environ["AI_MODEL"]
OPENROUTER = "https://openrouter.ai/api/v1"
API = f"https://api.telegram.org/bot{TOKEN}"

app = Flask(__name__)


def set_webhook():
    base = os.environ.get("RENDER_EXTERNAL_URL")
    if base:
        requests.post(
            f"{API}/setWebhook",
            json={"url": f"{base}/webhook", "secret_token": SECRET},
            timeout=10,
        )


set_webhook()


def send(chat_id, text):
    requests.post(
        f"{API}/sendMessage",
        json={"chat_id": chat_id, "text": text[:4000]},
        timeout=10,
    )


def call_model(model, prompt):
    r = requests.post(
        f"{OPENROUTER}/chat/completions",
        headers={"Authorization": f"Bearer {AI_KEY}"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        timeout=45,
    )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    if not text:
        raise ValueError("empty response")
    return text


def free_models():
    r = requests.get(f"{OPENROUTER}/models", timeout=15)
    r.raise_for_status()
    return [m["id"] for m in r.json()["data"] if m["id"].endswith(":free")]


def ask_llm(prompt):
    try:
        return AI_MODEL, call_model(AI_MODEL, prompt)
    except Exception as e:
        print(f"{AI_MODEL} failed: {e}")
    tried = 0
    for model in free_models():
        if model == AI_MODEL:
            continue
        try:
            return model, call_model(model, prompt)
        except Exception as e:
            print(f"{model} failed: {e}")
        tried += 1
        if tried == 3:
            break
    raise RuntimeError("все модели недоступны")


def answer(chat_id, prompt):
    try:
        model, text = ask_llm(prompt)
        send(chat_id, f"[{model}]\n{text}")
    except Exception as e:
        send(chat_id, f"ошибка: {e}")


@app.get("/")
def health():
    return "ok"


@app.post("/webhook")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        return "forbidden", 403
    msg = (request.get_json(silent=True) or {}).get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    if chat_id != OWNER:
        print(f"ignored message from chat {chat_id}")
        return "ok"
    text = msg.get("text") or ""
    if text.startswith("/ask "):
        threading.Thread(target=answer, args=(chat_id, text[5:]), daemon=True).start()
    elif text:
        send(chat_id, f"эхо: {text}")
    return "ok"
