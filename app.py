import base64
import json
import os
import re
import secrets
import threading
import time

import requests
from flask import Flask, request
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER = int(os.environ["OWNER_CHAT_ID"])
SECRET = os.environ["WEBHOOK_SECRET"]
AI_KEY = os.environ["AI_API_KEY"]
AI_MODEL = os.environ["AI_MODEL"]
OPENROUTER = "https://openrouter.ai/api/v1"
GITHUB = "https://api.github.com"
RENDER = "https://api.render.com/v1"
OWN_GH = os.environ.get("GITHUB_TOKEN")
OWN_RENDER = os.environ.get("RENDER_API_KEY")
RENDER_GH_NOTE = (
    "Важно: Render-аккаунт должен быть связан с GitHub-аккаунтом бота и иметь доступ "
    "ко всем репозиториям (All repositories), иначе Render не увидит приватный репозиторий."
)
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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


BTN_OWN = "🙋 Бот для меня"
BTN_NEW = "🤖 Создать бота"
BTN_ASK = "💬 Спросить LLM"
BTN_EDIT = "✏️ Изменить бота"
BTN_APPLY = "✅ Применить"
BTN_CANCEL = "Отмена"
BTN_PRIVATE = "Только мне"
BTN_PUBLIC = "Всем"
BTN_SHEETS = "📊 Google Таблица"
BTN_NOINT = "Без интеграций"
MENU = [[BTN_OWN], [BTN_NEW], [BTN_EDIT], [BTN_ASK]]
CANCEL = [[BTN_CANCEL]]
APPLY = [[BTN_APPLY], [BTN_CANCEL]]
AUDIENCE = [[BTN_PRIVATE, BTN_PUBLIC], [BTN_CANCEL]]
INTEGRATIONS = [[BTN_SHEETS], [BTN_NOINT], [BTN_CANCEL]]
TOKEN_PROMPT = (
    "Теперь токен бота: создайте нового в @BotFather и пришлите токен. "
    "Сообщение с токеном я удалю."
)
SHEETS_HELP = (
    "Google Таблица. Нужен ключ сервисного аккаунта:\n"
    "1. console.cloud.google.com → создайте проект (или выберите существующий).\n"
    "2. APIs & Services → Library → Google Sheets API → Enable.\n"
    "3. IAM & Admin → Service Accounts → Create service account (роли не нужны).\n"
    "4. Откройте созданный аккаунт → Keys → Add key → Create new key → JSON. Файл скачается.\n"
    "5. Откройте файл, скопируйте всё содержимое и пришлите сюда. Сообщение удалю."
)
state = {"step": None}
last_update = {"id": 0}
TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
CHUNK_WINDOW = 10  # seconds: Telegram splits long texts into several messages


def send(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text[:4000]}
    if keyboard:
        payload["reply_markup"] = {"keyboard": keyboard, "resize_keyboard": True}
    requests.post(f"{API}/sendMessage", json=payload, timeout=10)


def delete_message(chat_id, message_id):
    try:
        requests.post(
            f"{API}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=10,
        )
    except Exception as e:
        print(f"deleteMessage failed: {e}", flush=True)


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
        send(chat_id, f"[{model}]\n{text}", MENU)
    except Exception as e:
        send(chat_id, f"ошибка: {e}", MENU)


def gh(token, method, path, **kwargs):
    return requests.request(
        method,
        f"{GITHUB}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=20,
        **kwargs,
    )


def render_api(key, method, path, **kwargs):
    return requests.request(
        method,
        f"{RENDER}{path}",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        timeout=30,
        **kwargs,
    )


def checked(r, what):
    if not r.ok:
        raise RuntimeError(f"{what}: {r.status_code} {r.text[:200]}")
    return r.json()


def push_file(token, repo, path, content):
    checked(
        gh(
            token,
            "PUT",
            f"/repos/{repo}/contents/{path}",
            json={
                "message": f"add {path}",
                "content": base64.b64encode(content.encode()).decode(),
            },
        ),
        f"запись {path}",
    )


def create_service(key, name, repo, env):
    owner = checked(render_api(key, "GET", "/owners"), "owners")[0]
    owner_id = (owner.get("owner") or owner)["id"]
    return checked(
        render_api(
            key,
            "POST",
            "/services",
            json={
                "type": "web_service",
                "name": name,
                "ownerId": owner_id,
                "repo": f"https://github.com/{repo}",
                "autoDeploy": "no",
                "envVars": [{"key": k, "value": v} for k, v in env.items()],
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


def wait_deploy(key, created):
    final = ("live", "build_failed", "update_failed", "canceled", "pre_deploy_failed", "deactivated")
    status = None
    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(10)
        deploy = checked(
            render_api(
                key, "GET", f"/services/{created['service']['id']}/deploys/{created['deployId']}"
            ),
            "статус деплоя",
        )
        status = deploy["status"]
        if status in final:
            break
    return status


def make_spec(spec):
    try:
        _, out = ask_llm(
            "Ты создаёшь Telegram-бота по ТЗ. Верни ТОЛЬКО JSON вида "
            '{"name": "короткий слаг латиницей до 20 символов", '
            '"prompt": "системный промпт бота на языке ТЗ"}.\n\nТЗ: ' + spec
        )
        data = json.loads(out[out.index("{") : out.rindex("}") + 1])
        slug = re.sub(r"[^a-z0-9-]", "", str(data["name"]).lower())[:20].strip("-")
        prompt = str(data["prompt"]).strip()
        if not slug or not prompt:
            raise ValueError("empty name or prompt")
    except Exception as e:
        print(f"make_spec fallback: {e}", flush=True)
        slug, prompt = "bot", spec
    return f"{slug}-{secrets.token_hex(2)}", prompt


def new_bot(chat_id, spec, bot_token, username, gh_token, render_key, ai_key, extra_env):
    try:
        send(chat_id, f"все данные приняты (@{username}), собираю бота...")
        name, prompt = make_spec(spec)
        if "SHEET_ID" in extra_env:
            prompt += "\n\nВ ходе диалога вежливо выясни имя клиента и номер его телефона."
        created_repo = checked(
            gh(gh_token, "POST", "/user/repos", json={"name": name, "private": True, "auto_init": True}),
            "создание репо",
        )
        repo = created_repo["full_name"]
        time.sleep(2)
        with open(os.path.join(BASE_DIR, "bot_template.py"), encoding="utf-8") as f:
            push_file(gh_token, repo, "app.py", f.read())
        with open(os.path.join(BASE_DIR, "requirements.txt"), encoding="utf-8") as f:
            push_file(gh_token, repo, "requirements.txt", f.read())
        push_file(gh_token, repo, "prompt.txt", prompt)
        created = create_service(
            render_key,
            name,
            repo,
            {
                "PYTHON_VERSION": "3.11.9",
                "TELEGRAM_TOKEN": bot_token,
                "OWNER_CHAT_ID": str(OWNER),
                "WEBHOOK_SECRET": secrets.token_hex(16),
                "AI_API_KEY": ai_key,
                "AI_MODEL": AI_MODEL,
                **extra_env,
            },
        )
        send(chat_id, f"сервис {name} создан, жду деплой...")
        status = wait_deploy(render_key, created)
        report = (
            f"статус деплоя: {status}\nбот: @{username}\n"
            f"репо: https://github.com/{repo}\nсервис: {created['service']['serviceDetails']['url']}"
        )
        if "SHEET_ID" in extra_env:
            report += f"\nтаблица: https://docs.google.com/spreadsheets/d/{extra_env['SHEET_ID']}"
        if status != "live":
            report += "\n\n" + RENDER_GH_NOTE
        send(chat_id, report, MENU)
    except Exception as e:
        send(chat_id, f"ошибка: {e}", MENU)


@app.get("/")
def health():
    return "ok"


def reset():
    state.clear()
    state["step"] = None


def check_bot_token(token):
    try:
        me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10).json()
        return me["result"]["username"] if me.get("ok") else None
    except Exception:
        return None


def check_github(token):
    try:
        r = gh(token, "GET", "/user")
        return r.json()["login"] if r.ok else None
    except Exception:
        return None


def check_render(key):
    try:
        return render_api(key, "GET", "/owners").ok
    except Exception:
        return False


def check_openrouter(key):
    try:
        r = requests.get(f"{OPENROUTER}/auth/key", headers={"Authorization": f"Bearer {key}"}, timeout=10)
        return r.ok
    except Exception:
        return False


def parse_sa(text):
    try:
        info = json.loads(text)
        if info.get("type") != "service_account" or not info.get("private_key") or not info.get("client_email"):
            return None
        return json.dumps(info, separators=(",", ":")), info["client_email"]
    except Exception:
        return None


def parse_sheet_id(text):
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
    return m.group(1) if m else text.strip()


def sheet_error(r, email):
    if r.status_code == 404:
        return "Таблица не найдена. Проверьте ссылку и пришлите её ещё раз."
    if r.status_code == 403:
        if "SERVICE_DISABLED" in r.text or "has not been used" in r.text:
            return (
                "Google Sheets API не включён в проекте: APIs & Services → Library → "
                "Google Sheets API → Enable. Затем пришлите ссылку ещё раз."
            )
        return (
            f"Нет доступа на редактирование. Откройте таблицу → Настройки доступа, добавьте {email} "
            "с ролью «Редактор» и пришлите ссылку ещё раз."
        )
    return f"Google ответил {r.status_code}: {r.text[:150]}"


def check_sheet(sa_json, sheet_id, email):
    """Returns (title, None) if the sheet is readable and writable, else (None, error text)."""
    try:
        creds = service_account.Credentials.from_service_account_info(json.loads(sa_json), scopes=[SHEETS_SCOPE])
        session = AuthorizedSession(creds)
        r = session.get(f"{SHEETS_API}/{sheet_id}", params={"fields": "properties.title"}, timeout=15)
        if not r.ok:
            return None, sheet_error(r, email)
        title = r.json()["properties"]["title"]
        r = session.post(
            f"{SHEETS_API}/{sheet_id}:batchUpdate",
            json={
                "requests": [
                    {"updateSpreadsheetProperties": {"properties": {"title": title}, "fields": "title"}}
                ]
            },
            timeout=15,
        )
        if not r.ok:
            return None, sheet_error(r, email)
        return title, None
    except Exception as e:
        return None, f"Не удалось проверить таблицу: {e}"


def edit_list(chat_id):
    try:
        bots = {}
        services = checked(render_api(OWN_RENDER, "GET", "/services", params={"limit": 100}), "список сервисов")
        for item in services:
            svc = item.get("service") or item
            repo = (svc.get("repo") or "").removeprefix("https://github.com/").removesuffix(".git")
            if svc.get("type") != "web_service" or not repo:
                continue
            if gh(OWN_GH, "GET", f"/repos/{repo}/contents/prompt.txt").ok:
                bots[svc["name"]] = {"repo": repo, "service_id": svc["id"]}
        if not bots:
            send(chat_id, "Ботов, созданных через meta-agent, в ваших аккаунтах не нашлось.", MENU)
            return
        state.update(step="edit_pick", bots=bots)
        send(chat_id, "Какого бота изменить?", [[name] for name in bots] + CANCEL)
    except Exception as e:
        reset()
        send(chat_id, f"ошибка: {e}", MENU)


def edit_prepare(chat_id, target, change):
    try:
        f = checked(gh(OWN_GH, "GET", f"/repos/{target['repo']}/contents/prompt.txt"), "чтение prompt.txt")
        current = base64.b64decode(f["content"]).decode("utf-8")
        _, out = ask_llm(
            "Вот системный промпт Telegram-бота:\n\n" + current
            + "\n\nВнеси в него изменение: " + change
            + "\n\nСохрани все остальные требования. Верни ТОЛЬКО полный обновлённый промпт, "
            "без пояснений и без кавычек."
        )
        new = out.strip()
        if new.startswith("```"):
            new = new.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if len(new) < 20:
            raise RuntimeError("модель вернула пустой промпт")
        state.update(step="edit_confirm", new_prompt=new, sha=f["sha"])
        shown = new if len(new) <= 3300 else new[:3300] + "…\n(показано начало)"
        send(chat_id, f"Новый промпт:\n\n{shown}\n\nПрименить?", APPLY)
    except Exception as e:
        reset()
        send(chat_id, f"ошибка: {e}", MENU)


def edit_apply(chat_id, data):
    try:
        target = data["target"]
        checked(
            gh(
                OWN_GH,
                "PUT",
                f"/repos/{target['repo']}/contents/prompt.txt",
                json={
                    "message": "update prompt.txt",
                    "content": base64.b64encode(data["new_prompt"].encode()).decode(),
                    "sha": data["sha"],
                },
            ),
            "запись prompt.txt",
        )
        send(chat_id, "Промпт сохранён в репозитории, запускаю деплой...")
        deploy = checked(render_api(OWN_RENDER, "POST", f"/services/{target['service_id']}/deploys", json={}), "запуск деплоя")
        status = wait_deploy(OWN_RENDER, {"service": {"id": target["service_id"]}, "deployId": deploy["id"]})
        send(chat_id, f"Статус деплоя: {status}", MENU)
    except Exception as e:
        send(chat_id, f"ошибка: {e}", MENU)


def launch(chat_id):
    data = dict(state)
    reset()
    extra_env = {"PUBLIC": "1" if data.get("public") else "0"}
    if data.get("sheet_id"):
        extra_env.update(GOOGLE_SA_JSON=data["sa_json"], SHEET_ID=data["sheet_id"])
    threading.Thread(
        target=new_bot,
        args=(
            chat_id,
            data["spec"],
            data["bot_token"],
            data["username"],
            data["gh_token"],
            data["render_key"],
            data["ai_key"],
            extra_env,
        ),
        daemon=True,
    ).start()


def handle(chat_id, message_id, text):
    step = state.get("step")
    if text in (BTN_CANCEL, "/start", "/menu"):
        reset()
        send(chat_id, "Выберите действие:", MENU)
    elif text == BTN_OWN:
        reset()
        if not (OWN_GH and OWN_RENDER):
            send(
                chat_id,
                "Для этого режима в env сервиса meta-agent нужны GITHUB_TOKEN и RENDER_API_KEY "
                "(ваши). Добавьте их на Render и повторите.",
                MENU,
            )
        else:
            state.update(step="spec", mode="own", gh_token=OWN_GH, render_key=OWN_RENDER, ai_key=AI_KEY)
            send(chat_id, "Бот для вас, в ваших аккаунтах. Опишите ТЗ: что бот должен делать и как отвечать.", CANCEL)
    elif text == BTN_NEW:
        reset()
        state.update(step="spec", mode="other")
        send(
            chat_id,
            "Бот в чужие аккаунты. Опишите ТЗ: что бот должен делать и как отвечать.\n\n" + RENDER_GH_NOTE,
            CANCEL,
        )
    elif text == BTN_EDIT:
        reset()
        if not (OWN_GH and OWN_RENDER):
            send(
                chat_id,
                "Для этого режима в env сервиса meta-agent нужны GITHUB_TOKEN и RENDER_API_KEY "
                "(ваши). Добавьте их на Render и повторите.",
                MENU,
            )
        else:
            send(chat_id, "Ищу ваших ботов...", CANCEL)
            threading.Thread(target=edit_list, args=(chat_id,), daemon=True).start()
    elif text == BTN_ASK:
        reset()
        state["step"] = "ask"
        send(chat_id, "Что спросить у LLM?", CANCEL)
    elif step == "spec" and text:
        state.update(step="audience", spec=text, spec_at=time.time())
        send(
            chat_id,
            "Кто будет общаться с ботом? «Всем» — бот отвечает любому, кто ему напишет "
            "(например, клиентам). «Только мне» — только вам.",
            AUDIENCE,
        )
    elif step == "audience" and text:
        if text in (BTN_PRIVATE, BTN_PUBLIC):
            state.update(step="integration", public=(text == BTN_PUBLIC))
            send(
                chat_id,
                "С чем интегрировать бота? Сейчас в каталоге: 📊 Google Таблица "
                "(запись имени, телефона и диалога клиента).",
                INTEGRATIONS,
            )
        elif TOKEN_RE.match(text.strip()):
            delete_message(chat_id, message_id)
            send(chat_id, "Токен понадобится позже, я его удалил. Сначала выберите, кто будет общаться с ботом.", AUDIENCE)
        elif time.time() - state["spec_at"] < CHUNK_WINDOW:
            state["spec"] += "\n" + text  # Telegram splits long texts into several messages
            state["spec_at"] = time.time()
        else:
            send(chat_id, "Выберите вариант кнопкой.", AUDIENCE)
    elif step == "integration" and text:
        if text == BTN_NOINT:
            state["step"] = "bot_token"
            send(chat_id, TOKEN_PROMPT, CANCEL)
        elif text == BTN_SHEETS:
            state["step"] = "sa_json"
            send(chat_id, SHEETS_HELP, CANCEL)
        else:
            send(chat_id, "Такой интеграции пока нет в каталоге. Выберите кнопкой.", INTEGRATIONS)
    elif step == "sa_json" and text:
        delete_message(chat_id, message_id)
        parsed = parse_sa(text)
        if not parsed:
            send(
                chat_id,
                "Не похоже на ключ сервисного аккаунта. Нужно всё содержимое JSON-файла "
                "(в нём есть client_email и private_key). Пришлите ещё раз.",
                CANCEL,
            )
        else:
            sa_json, email = parsed
            state.update(step="sheet", sa_json=sa_json, sa_email=email)
            send(
                chat_id,
                f"Ключ принят. Создайте Google Таблицу, откройте Настройки доступа, добавьте {email} "
                "с ролью «Редактор» и пришлите ссылку на таблицу.",
                CANCEL,
            )
    elif step == "sheet" and text:
        sheet_id = parse_sheet_id(text)
        title, error = check_sheet(state["sa_json"], sheet_id, state["sa_email"])
        if error:
            send(chat_id, error, CANCEL)
        else:
            state.update(step="bot_token", sheet_id=sheet_id)
            send(chat_id, f"Таблица «{title}» доступна для записи.\n\n" + TOKEN_PROMPT, CANCEL)
    elif step == "bot_token" and text:
        token = text.strip()
        delete_message(chat_id, message_id)
        username = check_bot_token(token)
        if token == TOKEN:
            send(chat_id, "Это токен самого meta-agent. Нужен токен нового бота из @BotFather.", CANCEL)
        elif not username:
            send(chat_id, "Telegram не принял токен. Проверьте и пришлите ещё раз.", CANCEL)
        else:
            state.update(bot_token=token, username=username)
            if state.get("mode") == "own":
                launch(chat_id)
            else:
                state["step"] = "gh_token"
                send(
                    chat_id,
                    f"@{username} принят. Теперь токен GitHub того аккаунта, куда положить код бота. "
                    "Создать: github.com/settings/tokens?type=beta, доступ All repositories, "
                    "права Contents и Administration: Read and write. Сообщение удалю.",
                    CANCEL,
                )
    elif step == "gh_token" and text:
        delete_message(chat_id, message_id)
        login = check_github(text.strip())
        if not login:
            send(chat_id, "GitHub не принял токен. Проверьте и пришлите ещё раз.", CANCEL)
        else:
            state.update(step="render_key", gh_token=text.strip())
            send(
                chat_id,
                f"GitHub: {login}. Теперь ключ Render того аккаунта, где бот будет работать "
                "(Dashboard → Account Settings → API Keys). Сообщение удалю.\n\n" + RENDER_GH_NOTE,
                CANCEL,
            )
    elif step == "render_key" and text:
        delete_message(chat_id, message_id)
        if not check_render(text.strip()):
            send(chat_id, "Render не принял ключ. Проверьте и пришлите ещё раз.", CANCEL)
        else:
            state.update(step="ai_key", render_key=text.strip())
            send(
                chat_id,
                "Последнее: ключ OpenRouter для этого бота (openrouter.ai/keys → Create Key). "
                "Бесплатных моделей достаточно. Сообщение удалю.",
                CANCEL,
            )
    elif step == "ai_key" and text:
        delete_message(chat_id, message_id)
        if not check_openrouter(text.strip()):
            send(chat_id, "OpenRouter не принял ключ. Проверьте и пришлите ещё раз.", CANCEL)
        else:
            state["ai_key"] = text.strip()
            launch(chat_id)
    elif step == "edit_pick" and text:
        if text in state["bots"]:
            state.update(step="edit_request", target=state["bots"][text])
            send(chat_id, f"Бот {text}. Что изменить или добавить в его поведении? Опишите словами.", CANCEL)
        else:
            send(chat_id, "Выберите бота кнопкой.", [[name] for name in state["bots"]] + CANCEL)
    elif step == "edit_request" and text:
        state["step"] = "edit_working"
        send(chat_id, "Переписываю промпт...", CANCEL)
        threading.Thread(target=edit_prepare, args=(chat_id, state["target"], text), daemon=True).start()
    elif step == "edit_confirm" and text == BTN_APPLY:
        data = dict(state)
        reset()
        threading.Thread(target=edit_apply, args=(chat_id, data), daemon=True).start()
    elif step == "ask" and text:
        reset()
        threading.Thread(target=answer, args=(chat_id, text), daemon=True).start()
    else:
        send(chat_id, "Выберите действие:", MENU)


@app.post("/webhook")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        return "forbidden", 403
    update = request.get_json(silent=True) or {}
    uid = update.get("update_id", 0)
    if uid <= last_update["id"]:
        return "ok"
    last_update["id"] = uid
    msg = update.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    if chat_id != OWNER:
        print(f"ignored message from chat {chat_id}", flush=True)
        return "ok"
    handle(chat_id, msg.get("message_id"), msg.get("text") or "")
    return "ok"
