import base64
import os
import threading
import time

import requests
from flask import Flask, request

TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER = int(os.environ["OWNER_CHAT_ID"])
SECRET = os.environ["WEBHOOK_SECRET"]
AI_KEY = os.environ["AI_API_KEY"]
AI_MODEL = os.environ["AI_MODEL"]
OPENROUTER = "https://openrouter.ai/api/v1"
GH_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB = "https://api.github.com"
RENDER_KEY = os.environ.get("RENDER_API_KEY")
RENDER = "https://api.render.com/v1"
HELLO_APP = (
    "from flask import Flask\n"
    "app = Flask(__name__)\n\n\n"
    '@app.get("/")\n'
    "def hi():\n"
    '    return "hello from test bot"\n'
)
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


def ask_llm(prompt):
    errors = []
    try:
        return AI_MODEL, call_model(AI_MODEL, prompt)
    except Exception as e:
        errors.append(f"{AI_MODEL}: {e}")
    tried = 0
    for model in free_models():
        if model == AI_MODEL:
            continue
        try:
            return model, call_model(model, prompt)
        except Exception as e:
            errors.append(f"{model}: {e}")
        tried += 1
        if tried == 3:
            break
    print("\n".join(errors), flush=True)
    raise RuntimeError("все модели недоступны:\n" + "\n".join(errors))


def answer(chat_id, prompt):
    try:
        model, text = ask_llm(prompt)
        send(chat_id, f"[{model}]\n{text}")
    except Exception as e:
        send(chat_id, f"ошибка: {e}")


def gh(method, path, **kwargs):
    return requests.request(
        method,
        f"{GITHUB}{path}",
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        timeout=20,
        **kwargs,
    )


def test_repo(chat_id):
    try:
        if not GH_TOKEN:
            raise RuntimeError("GITHUB_TOKEN не задан")
        r = gh(
            "POST",
            "/user/repos",
            json={"name": "meta-agent-test", "private": True, "auto_init": True},
        )
        if not r.ok:
            raise RuntimeError(f"создание репо: {r.status_code} {r.text[:200]}")
        repo = r.json()["full_name"]
        time.sleep(2)
        r = gh(
            "PUT",
            f"/repos/{repo}/contents/hello.txt",
            json={
                "message": "add hello.txt",
                "content": base64.b64encode(b"hello from meta-agent").decode(),
            },
        )
        if not r.ok:
            raise RuntimeError(f"запись файла: {r.status_code} {r.text[:200]}")
        send(chat_id, f"готово: https://github.com/{repo}")
    except Exception as e:
        send(chat_id, f"ошибка: {e}")


def render_api(method, path, **kwargs):
    return requests.request(
        method,
        f"{RENDER}{path}",
        headers={"Authorization": f"Bearer {RENDER_KEY}", "Accept": "application/json"},
        timeout=30,
        **kwargs,
    )


def checked(r, what):
    if not r.ok:
        raise RuntimeError(f"{what}: {r.status_code} {r.text[:200]}")
    return r.json()


def test_deploy(chat_id):
    try:
        if not (RENDER_KEY and GH_TOKEN):
            raise RuntimeError("нужны GITHUB_TOKEN и RENDER_API_KEY")
        login = checked(gh("GET", "/user"), "github user")["login"]
        repo = f"{login}/meta-agent-test"
        for name, content in [("app.py", HELLO_APP), ("requirements.txt", "flask\ngunicorn\n")]:
            checked(
                gh(
                    "PUT",
                    f"/repos/{repo}/contents/{name}",
                    json={
                        "message": f"add {name}",
                        "content": base64.b64encode(content.encode()).decode(),
                    },
                ),
                f"запись {name}",
            )
        owner = checked(render_api("GET", "/owners"), "owners")[0]
        owner_id = (owner.get("owner") or owner)["id"]
        created = checked(
            render_api(
                "POST",
                "/services",
                json={
                    "type": "web_service",
                    "name": "meta-agent-test",
                    "ownerId": owner_id,
                    "repo": f"https://github.com/{repo}",
                    "autoDeploy": "no",
                    "envVars": [{"key": "PYTHON_VERSION", "value": "3.11.9"}],
                    "serviceDetails": {
                        "runtime": "python",
                        "plan": "free",
                        "region": "oregon",
                        "envSpecificDetails": {
                            "buildCommand": "pip install -r requirements.txt",
                            "startCommand": "gunicorn app:app",
                        },
                    },
                },
            ),
            "создание сервиса",
        )
        service = created["service"]
        url = service["serviceDetails"]["url"]
        send(chat_id, f"сервис создан, жду деплой: {url}")
        final = ("live", "build_failed", "update_failed", "canceled", "pre_deploy_failed", "deactivated")
        status = None
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(10)
            deploy = checked(
                render_api("GET", f"/services/{service['id']}/deploys/{created['deployId']}"),
                "статус деплоя",
            )
            status = deploy["status"]
            if status in final:
                break
        send(chat_id, f"статус деплоя: {status}\n{url}")
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
    elif text == "/test_repo":
        threading.Thread(target=test_repo, args=(chat_id,), daemon=True).start()
    elif text == "/test_deploy":
        threading.Thread(target=test_deploy, args=(chat_id,), daemon=True).start()
    elif text:
        send(chat_id, f"эхо: {text}")
    return "ok"
