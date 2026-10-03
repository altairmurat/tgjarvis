import os
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

import asyncio
import traceback
import secrets, hashlib, base64
from email.message import EmailMessage
from telethon import TelegramClient, events, Button, types
from openai import AsyncOpenAI
import datetime
import io, re, json
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
import urllib.parse

from env import API_ID, API_HASH, BOT_TOKEN, OPENAI_API, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, REDIRECT_URI, WEBAPP_URL
from llm import ask_gpt, process_telegram_image
from database import SessionLocal, engine
import models
import auto_login
import dgist_monitor
import dgist_bot_handlers
from dgist_bot_handlers import register_dgist_handlers, handle_dgist_conversation_step

app = FastAPI()

client = TelegramClient('session_bot_korean', API_ID, API_HASH)
ai_client = AsyncOpenAI(api_key=OPENAI_API)

user_states = {}
pending_photos = {}
pending_textvoice = {}
draft_cache = {}  # {draft_id: telegram_user_id}
text_cache = {}   # {draft_id: email_body}


# =====================================================================
# GMAIL OAUTH (Web app flow + PKCE)
# =====================================================================

SCOPES = ["https://www.googleapis.com/auth/gmail.compose",
          "https://www.googleapis.com/auth/gmail.readonly"]

CLIENT_CONFIG = {
    "web": {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [REDIRECT_URI],
    }
}

pkce_store = {}


def generate_pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(telegram_user_id: int) -> str:
    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    verifier, challenge = generate_pkce_pair()
    pkce_store[telegram_user_id] = verifier
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=str(telegram_user_id),
        code_challenge=challenge,
        code_challenge_method="S256",
    )
    return auth_url


# =====================================================================
# TOKEN STORAGE (DB <-> google Credentials)
# =====================================================================

def delete_user_token(telegram_user_id: int):
    db = SessionLocal()
    try:
        acc = db.query(models.GoogleAccount).filter_by(telegram_user_id=telegram_user_id).first()
        if acc:
            acc.token_json = None
            db.commit()
    except Exception as e:
        db.rollback()
        print(f"Error deleting token for {telegram_user_id}: {e}")
    finally:
        db.close()


def save_user_token(telegram_user_id: int, creds: Credentials):
    db = SessionLocal()
    try:
        acc = db.query(models.GoogleAccount).filter_by(telegram_user_id=telegram_user_id).first()
        if not acc:
            acc = models.GoogleAccount(telegram_user_id=telegram_user_id)
            db.add(acc)
        acc.token_json = creds.to_json()
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Error saving token for {telegram_user_id}: {e}")
        raise
    finally:
        db.close()


def load_user_creds(telegram_user_id: int) -> Credentials | None:
    db = SessionLocal()
    try:
        acc = db.query(models.GoogleAccount).filter_by(telegram_user_id=telegram_user_id).first()
        if not acc or not acc.token_json:
            return None

        creds = Credentials.from_authorized_user_info(json.loads(acc.token_json))
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                acc.token_json = creds.to_json()
                db.commit()
            except RefreshError as e:
                print(f"Token refresh failed for {telegram_user_id}: {e}")
                db.rollback()
                delete_user_token(telegram_user_id)
                return None
        return creds
    except Exception as e:
        print(f"Error loading creds for {telegram_user_id}: {e}")
        return None
    finally:
        db.close()


def get_gmail_client_for(telegram_user_id: int):
    creds = load_user_creds(telegram_user_id)
    return build("gmail", "v1", credentials=creds) if creds else None


# =====================================================================
# OAuth HTTP routes (FastAPI)
# =====================================================================

@app.get("/gmail/connect/{telegram_user_id}")
async def gmail_connect(telegram_user_id: int):
    delete_user_token(telegram_user_id)
    return RedirectResponse(build_auth_url(telegram_user_id))


@app.get("/gmail/callback")
async def gmail_callback(request: FastAPIRequest):
    try:
        code = request.query_params.get("code")
        telegram_user_id = int(request.query_params.get("state"))

        flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI)
        flow.code_verifier = pkce_store.pop(telegram_user_id, None)
        flow.fetch_token(code=code)
        save_user_token(telegram_user_id, flow.credentials)

        try:
            await client.send_message(telegram_user_id, "✅ Gmail подключен! Теперь можешь просить меня писать письма.")
        except Exception:
            pass

        return HTMLResponse("<h2>Готово! Можешь закрыть эту вкладку и вернуться в Telegram.</h2>")
    except Exception as e:
        print("GMAIL CALLBACK ERROR:", traceback.format_exc())
        return HTMLResponse(f"<h2>Ошибка: {e}</h2>", status_code=500)


# =====================================================================
# EMAIL DRAFTING & KEYBOARD HELPER
# =====================================================================

EMAIL_TOOLS = [{
    "type": "function",
    "function": {
        "name": "create_email_draft",
        "description": "Подготовить черновик письма, когда пользователь просит написать/составить email, письмо, сообщение или просто скажет 'отправь / напиши / уведоми / напомни' кому-либо на том языке, на котором делает запрос пользователь",
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_name": {"type": "string", "description": "Имя получателя, стандартный вид"},
                "recipient_email": {"type": "string", "description": "Email получателя, если пользователь его явно указал в сообщении, иначе пусто"},
                "topic": {"type": "string", "description": "О чём письмо"},
                "sender_name": {"type": "string", "description": "Имя отправителя, напиши от имени пользователя, если пользователь явно указал, иначе пусто"},
            },
            "required": ["recipient_name", "topic"],
        },
    },
}]


def find_email_in_history(gmail, name: str) -> str | None:
    if not name or not name.strip():
        return None
    name_clean = name.strip()
    first_name = name_clean.split()[0].lower()

    my_email = ""
    try:
        profile = gmail.users().getProfile(userId="me").execute()
        my_email = (profile.get("emailAddress") or "").lower()
    except Exception:
        pass

    try:
        res = gmail.users().messages().list(userId="me", q=f'"{name_clean}"', maxResults=5).execute()
        for m in res.get("messages", []):
            msg = gmail.users().messages().get(
                userId="me", id=m["id"], format="metadata", metadataHeaders=["To", "From"]
            ).execute()
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            
            # Prioritize To header, then From
            for header_val in (headers.get("To", ""), headers.get("From", "")):
                if not header_val:
                    continue
                emails = re.findall(r"[\w.+-]+@[\w.-]+", header_val)
                # Word boundary check for name
                if re.search(rf"\b{re.escape(first_name)}\b", header_val, re.IGNORECASE):
                    for email_addr in emails:
                        if email_addr.lower() != my_email:
                            return email_addr
    except Exception as e:
        print(f"Error finding email in history: {e}")
    return None


def create_gmail_draft(gmail, to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg['To'] = to
    msg['Subject'] = subject
    msg.set_content(body, charset='utf-8')

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft = gmail.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return draft["id"]


async def generate_email_text(topic: str, recipient: str, sendername: str) -> tuple[str, str]:
    r = await ai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content":
            f"Напиши определенное письмо, получатель: {recipient}. Тема сообщения: {topic}. Имя отправителя: {sendername}."
            f"Ответь строго JSON: {{\"subject\": \"...\", \"body\": \"...\"}}"}],
        response_format={"type": "json_object"},
    )
    data = json.loads(r.choices[0].message.content)
    return data.get("subject", "Без темы"), data.get("body", "")


def get_formatted_webapp_url(draft_id: str, message_id: int = 0, user_id: int = 0) -> str:
    """Формирует HTTPS URL для Telegram Mini App с ID сообщения и пользователя."""
    base_url = WEBAPP_URL.strip().rstrip("/")
    if not base_url.startswith("http://") and not base_url.startswith("https://"):
        base_url = f"https://{base_url}"
    query_parts = [f"draft_id={urllib.parse.quote(draft_id)}", f"message_id={message_id}"]
    if user_id:
        query_parts.append(f"user_id={user_id}")
    return f"{base_url}/webapp?{'&'.join(query_parts)}"


def make_draft_keyboard(draft_id: str, message_id: int, user_id: int = 0):
    webapp_url = get_formatted_webapp_url(draft_id, message_id, user_id)

    return [
        [
            Button.inline("✅ Отправить", f"send:{draft_id}"),
            Button.inline("❌ Отмена", f"cancel:{draft_id}")
        ],
        [
            types.KeyboardButtonWebView("✏️ Изменить текст", webapp_url)
        ]
    ]


async def send_draft_to_telegram(event, gmail, telegram_user_id, to_email, name, topic, sendername):
    subject, body = await generate_email_text(topic, name, sendername)
    draft_id = await asyncio.to_thread(create_gmail_draft, gmail, to_email, subject, body)
    draft_cache[draft_id] = telegram_user_id  
    text_cache[draft_id] = body 
    
    display_text = f"📧 Черновик для {name} ({to_email})\n\nТема: {subject}\n\n{body}"
    
    # Сначала отправляем сообщение, чтобы зафиксировать message.id
    msg = await event.reply(display_text)
    
    # Редактируем сообщение, навешивая клавиатуру с message.id и user_id
    buttons = make_draft_keyboard(draft_id, msg.id, telegram_user_id)
    await msg.edit(display_text, buttons=buttons)


async def try_handle_email_intent(event, user_id: int, user_message: str) -> bool:
    r = await ai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": user_message}],
        tools=EMAIL_TOOLS, tool_choice="auto",
    )
    msg = r.choices[0].message
    if not msg.tool_calls:
        return False

    gmail = await asyncio.to_thread(get_gmail_client_for, user_id)
    if not gmail:
        auth_url = build_auth_url(user_id)
        await event.reply(f"Сначала подключи (или переподключи) Gmail: {auth_url}")
        return True

    args = json.loads(msg.tool_calls[0].function.arguments)
    name = args.get("recipient_name") or ""
    topic = args.get("topic") or ""
    sendername = args.get("sender_name") or "Имя отсутствует, просто отправь без учета имени"

    try:
        email = args.get("recipient_email") or await asyncio.to_thread(find_email_in_history, gmail, name)

        if not email:
            user_states[user_id] = ("waiting_for_manual_email", name, topic, sendername)
            await event.reply(f"Не нашёл email для {name}. Пришли его почту одним сообщением.")
            return True

        await send_draft_to_telegram(event, gmail, user_id, email, name, topic, sendername)
    except RefreshError:
        delete_user_token(user_id)
        auth_url = build_auth_url(user_id)
        await event.reply(
            "⚠️ Твой доступ к Gmail протух или был отозван.\n"
            f"Пожалуйста, авторизуйся заново по ссылке: {auth_url}"
        )
    return True


@client.on(events.CallbackQuery)
async def on_callback(event):
    data = event.data.decode()
    if ":" not in data:
        return
        
    action, draft_id = data.split(":")
    telegram_user_id = draft_cache.get(draft_id) or event.sender_id
    gmail = await asyncio.to_thread(get_gmail_client_for, telegram_user_id) if telegram_user_id else None

    if not gmail:
        auth_url = build_auth_url(event.sender_id)
        await event.edit(
            "❌ Ошибка: Сессия Gmail не найдена или истёк токен.\n\n"
            f"Авторизуйся заново по ссылке: {auth_url}"
        )
        return

    try:
        if action == "send":
            await asyncio.to_thread(
                gmail.users().drafts().send(userId="me", body={"id": draft_id}).execute
            )
            await event.edit("✅ Отправлено")
        else:
            await asyncio.to_thread(
                gmail.users().drafts().delete(userId="me", id=draft_id).execute
            )
            await event.edit("❌ Отменено")
    except RefreshError:
        delete_user_token(telegram_user_id)
        auth_url = build_auth_url(telegram_user_id)
        await event.edit(
            "⚠️ Токен Gmail был отозван или протух.\n\n"
            f"Пройди авторизацию заново по ссылке: {auth_url}"
        )
    except Exception as e:
        err_msg = str(e)
        if "404" in err_msg:
            await event.edit("⚠️ Черновик уже отправлен или удалён.")
        else:
            await event.edit(f"Ошибка при выполнении действия: {e}")
    finally:
        draft_cache.pop(draft_id, None)
        text_cache.pop(draft_id, None)


# =====================================================================
# STARTUP / SHUTDOWN
# =====================================================================

@app.on_event("startup")
async def startup_event():
    try:
        models.Base.metadata.create_all(bind=engine)
        print("Database tables created successfully")
    except Exception as e:
        print(f"DB init error: {e}")

    await client.start(bot_token=BOT_TOKEN)

    register_dgist_handlers(client, user_states)

    asyncio.create_task(client.run_until_disconnected())

@app.on_event("shutdown")
async def shutdown_event():
    await client.disconnect()

@app.get("/")
async def ping():
    return {"status": "ok"}

# =====================================================================
# MISC DB HELPERS
# =====================================================================

def save_communication(username, usermessage):
    db = SessionLocal()
    try:
        db.add(models.Communication(username=username, usermessage=usermessage))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"save_communication error: {e}")
    finally:
        db.close()


def get_current_datetime():
    current_datetime = str(datetime.datetime.now()).split(" ")
    current_date, current_time = current_datetime[0], current_datetime[1][:5]
    return [current_date, current_time]

# =====================================================================
# EMAIL DRAFT EDITOR HANDLER (Mini App Endpoints)
# =====================================================================

class EditDraftModel(BaseModel):
    chat_id: int
    message_id: int
    draft_id: str
    new_text: str


@app.get("/get-text")
async def get_text(draft_id: str):
    body_text = text_cache.get(draft_id, "Текст не найден или устарел")
    return {"text": body_text}


@app.get("/webapp", response_class=HTMLResponse)
async def get_webapp(draft_id: str = "", message_id: int = 0, user_id: int = 0):
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background-color: var(--tg-theme-bg-color, #ffffff);
                color: var(--tg-theme-text-color, #000000);
                margin: 0; padding: 15px; display: flex; flex-direction: column; height: 90vh;
            }}
            h3 {{
                margin-top: 0;
            }}
            textarea {{
                width: 100%; flex-grow: 1;
                background-color: var(--tg-theme-secondary-bg-color, #f0f0f0);
                color: var(--tg-theme-text-color, #000000);
                border: 1px solid #ccc; border-radius: 8px; padding: 10px; font-size: 15px; box-sizing: border-box; resize: none;
            }}
            button {{
                margin-top: 15px; padding: 12px;
                background-color: var(--tg-theme-button-color, #2481cc);
                color: var(--tg-theme-button-text-color, #ffffff);
                border: none; border-radius: 8px; font-size: 16px; font-weight: bold; cursor: pointer;
            }}
        </style>
    </head>
    <body>
        <h3>Редактирование письма</h3>
        <textarea id="email-text" placeholder="Загрузка текста..."></textarea>
        <button id="save-btn">Сохранить изменения</button>

        <script>
            const tg = window.Telegram.WebApp;
            tg.ready();
            tg.expand();

            const urlParams = new URLSearchParams(window.location.search);
            const draftId = urlParams.get('draft_id') || "";
            const messageId = parseInt(urlParams.get('message_id') || "0");
            const paramUserId = parseInt(urlParams.get('user_id') || "0");

            async function loadText() {{
                try {{
                    const response = await fetch('/get-text?draft_id=' + encodeURIComponent(draftId));
                    const data = await response.json();
                    document.getElementById('email-text').value = data.text;
                }} catch(e) {{
                    document.getElementById('email-text').value = "Ошибка загрузки текста";
                }}
            }}
            loadText();

            document.getElementById('save-btn').addEventListener('click', async () => {{
                const updatedText = document.getElementById('email-text').value;
                const user_id = tg.initDataUnsafe.user?.id || paramUserId || 0;

                const response = await fetch('/update-draft', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        chat_id: user_id,
                        message_id: messageId,
                        draft_id: draftId,
                        new_text: updatedText
                    }})
                }});

                if (response.ok) {{
                    tg.close();
                }} else {{
                    alert("Ошибка сохранения!");
                }}
            }});
        </script>
    </body>
    </html>
    """
    return html_content


@app.post("/update-draft")
async def update_draft(data: EditDraftModel):
    text_cache[data.draft_id] = data.new_text
    
    telegram_user_id = draft_cache.get(data.draft_id) or (data.chat_id if data.chat_id != 0 else None)
    gmail = await asyncio.to_thread(get_gmail_client_for, telegram_user_id) if telegram_user_id else None
    
    subject_val = "Без темы"
    to_val = ""
    
    if gmail:
        try:
            # Получаем тему и получателя из черновика
            draft_info = await asyncio.to_thread(
                gmail.users().drafts().get(userId="me", id=data.draft_id, format="full").execute
            )
            headers = draft_info.get("message", {}).get("payload", {}).get("headers", [])
            to_val = next((h["value"] for h in headers if h.get("name", "").lower() == "to"), "")
            subject_val = next((h["value"] for h in headers if h.get("name", "").lower() == "subject"), "Без темы")
            
            # Собираем MIME-сообщение с гарантированным UTF-8
            msg = EmailMessage()
            msg['To'] = to_val
            msg['Subject'] = subject_val
            msg.set_content(data.new_text, charset='utf-8')
            
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            await asyncio.to_thread(
                gmail.users().drafts().update(userId="me", id=data.draft_id, body={"message": {"raw": raw}}).execute
            )
        except Exception as e:
            print(f"Error updating Gmail draft: {e}")

    # Мгновенно обновляем текст и клавиатуру в Telegram
    if data.message_id != 0 and telegram_user_id:
        try:
            new_display_text = f"📧 Черновик для {to_val}\n\nТема: {subject_val}\n\n{data.new_text}"
            buttons = make_draft_keyboard(data.draft_id, data.message_id, telegram_user_id)
            await client.edit_message(telegram_user_id, data.message_id, new_display_text, buttons=buttons)
        except Exception as e:
            print(f"Error editing Telegram message: {e}")

    return {"status": "success"}


# =====================================================================
# TELEGRAM HANDLERS
# =====================================================================

@client.on(events.NewMessage(pattern='/start'))
async def start_message(event):
    await event.respond('Привет! Меня зовут Кристина, я твой полноценный ассистент, проси меня о чем угодно!')


@client.on(events.NewMessage(pattern=r'^/(connectgmail|reconnect)'))
async def connect_gmail(event):
    user_id = event.sender_id
    delete_user_token(user_id)
    auth_url = build_auth_url(user_id)
    await event.respond(
        "⚠️ **Подключение / Переподключение Gmail**\n\n"
        "Нажми на ссылку ниже, чтобы авторизоваться и привязать почту заново:\n"
        f"{auth_url}"
    )


@client.on(events.NewMessage(pattern='/sendShak'))
async def sendmessage(event):
    user_id = event.sender_id
    user_states[user_id] = "waiting_for_submitmessage"
    await event.respond('send your message to shak')


@client.on(events.NewMessage)
async def necessary_task_handler(event):
    if not event.is_private:
        return

    user_id = event.sender_id
    sender = await event.get_sender()

    if event.text and event.text.startswith("/"):
        return

    state = user_states.get(user_id)
    
    if await handle_dgist_conversation_step(event, user_id, state, user_states):
        return
    
    if isinstance(state, tuple) and state[0] == "waiting_for_manual_email":
        _, name, topic, sendername = state
        email_match = re.search(r"[\w.+-]+@[\w.-]+", event.text or "")
        if email_match:
            gmail = await asyncio.to_thread(get_gmail_client_for, user_id)
            user_states.pop(user_id, None)
            if gmail:
                try:
                    await send_draft_to_telegram(event, gmail, user_id, email_match.group(0), name, topic, sendername)
                except RefreshError:
                    delete_user_token(user_id)
                    auth_url = build_auth_url(user_id)
                    await event.reply(f"Токен протух. Подключи Gmail заново: {auth_url}")
            else:
                auth_url = build_auth_url(user_id)
                await event.reply(f"Сессия не найдена. Подключи Gmail заново: {auth_url}")
        else:
            await event.reply("Это не похоже на email, попробуй ещё раз (или /cancel для отмены)")
        return

    elif event.text and event.text.startswith("!"):
        if user_id in user_states and user_states[user_id] == "waiting_for_submitmessage":
            parts = event.text[1:].split("/", 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                target_user, msg_body = parts[0].strip(), parts[1].strip()
                try:
                    await client.send_message(target_user, msg_body)
                    await event.reply("✅ Сообщение успешно отправлено!")
                except Exception as e:
                    await event.reply(f"❌ Не удалось отправить сообщение: {e}")
                finally:
                    user_states.pop(user_id, None)
            else:
                await event.reply("Формат сообщения неверный. Используй: `!username/текст сообщения`")
            return

    else:
        if event.message.voice:
            try:
                voice_bytes = await event.message.download_media(file=bytes)
                if not voice_bytes:
                    await event.reply("Не удалось скачать голосовое сообщение.")
                    return
                audio_file = io.BytesIO(voice_bytes)
                audio_file.name = "voice.ogg"
                response = await ai_client.audio.transcriptions.create(
                    model="gpt-4o-mini-transcribe", file=audio_file
                )
                transcribed_text = response.text.strip() if response.text else ""
                if transcribed_text:
                    try:
                        handled = await try_handle_email_intent(event, user_id, transcribed_text)
                        if not handled:
                            gpt_response = await ask_gpt(chat_text=transcribed_text, user_id=user_id)
                            await event.respond(gpt_response)
                            await asyncio.to_thread(
                                save_communication,
                                sender.username if sender else None,
                                transcribed_text
                            )
                    except Exception as e:
                        await event.respond(f"Не удалось обработать распознанный текст: {e}")
                else:
                    await event.reply("Не удалось распознать речь в голосовом сообщении.")
            except Exception as e:
                await event.reply(f"Ошибка при обработке голосового сообщения: {e}")
            finally:
                user_states.pop(user_id, None)

        elif event.photo:
            pending_photos[user_id] = event.message
            user_states[user_id] = "waiting_for_imageprompt"
            await event.respond("Получила вашу фотку! Что я должна сделать?")
            return

        elif user_id in user_states and user_states[user_id] == "waiting_for_imageprompt":
            if event.text:
                user_prompt = event.text
                photo_message = pending_photos.get(user_id)
                if photo_message:
                    await event.respond("Обрабатываю...")
                    try:
                        response = await process_telegram_image(photo_message, user_prompt)
                        await event.respond(response)
                    except Exception as e:
                        await event.respond(f"Ошибка: {e}")
                    user_states.pop(user_id, None)
                    pending_photos.pop(user_id, None)
                return
        else:
            user_message = event.text
            if not user_message:
                return
            try:
                handled = await try_handle_email_intent(event, user_id, user_message)
                if not handled:
                    response = await ask_gpt(chat_text=user_message, user_id=user_id)
                    await event.respond(response)
                    await asyncio.to_thread(
                        save_communication,
                        sender.username if sender else None,
                        user_message
                    )
            except Exception as e:
                await event.respond(f"Не получилось обработать сообщение. Ошибка: {e}")