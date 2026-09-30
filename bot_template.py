import json
import os
import re
import threading
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from flask import Flask, request
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER = int(os.environ["OWNER_CHAT_ID"])
SECRET = os.environ["WEBHOOK_SECRET"]
AI_KEY = os.environ["AI_API_KEY"]
AI_MODEL = os.environ["AI_MODEL"]
PUBLIC = os.environ.get("PUBLIC") == "1"
SHEET_ID = os.environ.get("SHEET_ID")
OPENROUTER = "https://openrouter.ai/api/v1"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
API = f"https://api.telegram.org/bot{TOKEN}"
PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{8,}\d")

with open(os.path.join(os.path.dirname(__file__), "prompt.txt"), encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

sheet = None
if SHEET_ID and os.environ.get("GOOGLE_SA_JSON"):
    sheet = AuthorizedSession(
        service_account.Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_SA_JSON"]),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
    )

chats = {}
reported = {"sheet": False}

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


def call_model(model, messages):
    r = requests.post(
        f"{OPENROUTER}/chat/completions",
        headers={"Authorization": f"Bearer {AI_KEY}"},
        json={"model": model, "messages": messages},
        timeout=45,
    )
    if not r.ok:
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
    text = r.json()["choices"][0]["message"]["content"]
    if not text:
        raise ValueError("empty response")
    return text


def free_models():
    r = requests.get(f"{OPENROUTER}/models", timeout=15)
    r.raise_for_status()
    return [m["id"] for m in r.json()["data"] if m["id"].endswith(":free")]


def ask_llm(messages):
    errors = []
    try:
        return call_model(AI_MODEL, messages)
    except Exception as e:
        errors.append(f"{AI_MODEL}: {e}")
    tried = 0
    for model in free_models():
        if model == AI_MODEL:
            continue
        try:
            return call_model(model, messages)
        except Exception as e:
            errors.append(f"{model}: {e}")
        tried += 1
        if tried == 3:
            break
    raise RuntimeError("все модели недоступны:\n" + "\n".join(errors))


def chat_state(chat_id):
    return chats.setdefault(
        chat_id,
        {
            "history": [],
            "transcript": [],
            "lock": threading.Lock(),
            "phone": "",
            "name": "",
            "name_tries": 0,
            "time": "",
            "row": None,
        },
    )


def extract_name(transcript):
    try:
        out = ask_llm(
            [
                {
                    "role": "user",
                    "content": "Из диалога определи имя клиента. Верни ТОЛЬКО JSON вида "
                    '{"name": "..."}; если имени нет, верни {"name": ""}.\n\n'
                    + "\n".join(transcript)[-6000:],
                }
            ]
        )
        data = json.loads(out[out.index("{") : out.rindex("}") + 1])
        return str(data.get("name", "")).strip()[:60]
    except Exception as e:
        print(f"extract_name failed: {e}", flush=True)
        return ""


def record(st, user_text):
    if not st["phone"]:
        m = PHONE_RE.search(user_text)
        if not m:
            return
        st["phone"] = m.group().strip()
        st["time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if not st["name"] and st["name_tries"] < 5:
        st["name_tries"] += 1
        st["name"] = extract_name(st["transcript"])
    row = [st["time"], st["name"], st["phone"], "\n".join(st["transcript"])[-45000:]]
    if st["row"] is None:
        r = sheet.post(
            f"{SHEETS}/{SHEET_ID}/values/A:D:append",
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json={"values": [row]},
            timeout=20,
        )
        if not r.ok:
            raise RuntimeError(f"{r.status_code} {r.text[:200]}")
        st["row"] = r.json()["updates"]["updatedRange"]
    else:
        r = sheet.put(
            f"{SHEETS}/{SHEET_ID}/values/{quote(st['row'], safe='')}",
            params={"valueInputOption": "RAW"},
            json={"values": [row]},
            timeout=20,
        )
        if not r.ok:
            raise RuntimeError(f"{r.status_code} {r.text[:200]}")


def report_sheet_error(e):
    print(f"sheet error: {e}", flush=True)
    if not reported["sheet"]:
        reported["sheet"] = True
        send(OWNER, f"Не удалось записать в таблицу: {e}")


def answer(chat_id, text):
    st = chat_state(chat_id)
    with st["lock"]:
        history = st["history"]
        history.append({"role": "user", "content": text})
        del history[:-20]
        try:
            out = ask_llm([{"role": "system", "content": SYSTEM_PROMPT}] + history)
        except Exception as e:
            history.pop()
            send(chat_id, f"ошибка: {e}")
            return
        history.append({"role": "assistant", "content": out})
        del history[:-20]
        st["transcript"] += [f"Клиент: {text}", f"Бот: {out}"]
        send(chat_id, out)
        if sheet:
            try:
                record(st, text)
            except Exception as e:
                report_sheet_error(e)


@app.get("/")
def health():
    return "ok"


@app.post("/webhook")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        return "forbidden", 403
    msg = (request.get_json(silent=True) or {}).get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    if not chat_id:
        return "ok"
    if chat_id != OWNER and not PUBLIC:
        print(f"ignored message from chat {chat_id}", flush=True)
        return "ok"
    text = msg.get("text")
    if text:
        threading.Thread(target=answer, args=(chat_id, text), daemon=True).start()
    return "ok"
