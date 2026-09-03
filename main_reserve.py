import os
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

import asyncio
import traceback
import secrets, hashlib, base64
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
from fastapi.responses import RedirectResponse, HTMLResponse
import urllib.parse

from env import API_ID, API_HASH, BOT_TOKEN, OPENAI_API, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, REDIRECT_URI
from llm import ask_gpt, process_telegram_image
from database import SessionLocal, engine
import models

app = FastAPI()

client = TelegramClient('session_bot_korean', API_ID, API_HASH)
ai_client = AsyncOpenAI(api_key=OPENAI_API)

user_states = {}
pending_photos = {}
pending_textvoice = {}
draft_cache = {}  # {draft_id: telegram_user_id} — чтобы знать, чьим gmail отправлять при нажатии кнопки


# =====================================================================
# GMAIL OAUTH (Web app flow + PKCE)
# =====================================================================
# Flow: /gmail/connect/<id> -> Google consent screen -> /gmail/callback
# -> token saved in DB, tied to the telegram_user_id via the `state` param.

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

pkce_store = {}  # telegram_user_id -> code_verifier, живёт только на время OAuth-хендшейка


def generate_pkce_pair():
    """Standard S256 PKCE pair: a random verifier + its sha256 challenge."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(telegram_user_id: int) -> str:
    """Build a per-user Google consent URL. `state` carries the telegram id
    so the callback knows whose token it just received."""
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
    """Wipe a user's stored token (used on reconnect, or when Google says invalid_grant)."""
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
    """Create/update the DB row holding this user's Gmail credentials."""
    db = SessionLocal()
    try:
        acc = db.query(models.GoogleAccount).filter_by(telegram_user_id=telegram_user_id).first()
        if not acc:
            acc = models.GoogleAccount(telegram_user_id=telegram_user_id)
            db.add(acc)
        acc.token_json = creds.to_json()
        db.commit()
    finally:
        db.close()


def load_user_creds(telegram_user_id: int) -> Credentials | None:
    """Fetch the user's token from DB, auto-refresh if expired,
    and drop it if the refresh fails (revoked access)."""
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
    """Ready-to-use Gmail API client, or None if the user isn't connected."""
    creds = load_user_creds(telegram_user_id)
    return build("gmail", "v1", credentials=creds) if creds else None


# =====================================================================
# OAuth HTTP routes (FastAPI, separate from the Telethon bot)
# =====================================================================

@app.get("/gmail/connect/{telegram_user_id}")
async def gmail_connect(telegram_user_id: int):
    delete_user_token(telegram_user_id)  # force a fresh consent/token each time
    return RedirectResponse(build_auth_url(telegram_user_id))


@app.get("/gmail/callback")
async def gmail_callback(request: FastAPIRequest):
    """Google redirects here after the user approves access."""
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
            pass  # bot may not be able to DM the user yet, not fatal

        return HTMLResponse("<h2>Готово! Можешь закрыть эту вкладку и вернуться в Telegram.</h2>")
    except Exception as e:
        print("GMAIL CALLBACK ERROR:", traceback.format_exc())
        return HTMLResponse(f"<h2>Ошибка: {e}</h2>", status_code=500)

# =====================================================================
# EMAIL DRAFTING (LLM tool-call -> find recipient -> Gmail draft -> confirm)
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
    """Best-effort search of the user's own Gmail history for `name`'s address."""
    res = gmail.users().messages().list(userId="me", q=f'"{name}"', maxResults=5).execute()
    for m in res.get("messages", []):
        msg = gmail.users().messages().get(
            userId="me", id=m["id"], format="metadata", metadataHeaders=["From", "To"]
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        for v in (headers.get("From", ""), headers.get("To", "")):
            match = re.search(r"[\w.+-]+@[\w.-]+", v)
            if match and name.split()[0].lower() in v.lower():
                return match.group(0)
    return None


def create_gmail_draft(gmail, to: str, subject: str, body: str) -> str:
    """Create a Gmail draft (not sent yet) and return its draft id."""
    raw = base64.urlsafe_b64encode(f"To: {to}\r\nSubject: {subject}\r\n\r\n{body}".encode()).decode()
    draft = gmail.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return draft["id"]


async def generate_email_text(topic: str, recipient: str, sendername: str) -> tuple[str, str, str]:
    """Ask GPT to write the subject + body for this email."""
    r = await ai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content":
            f"Напиши определенное письмо, получатель: {recipient}. Тема сообщения: {topic}. Имя отправителя: {sendername}."
            f"Ответь строго JSON: {{\"subject\": \"...\", \"body\": \"...\"}}"}],
        response_format={"type": "json_object"},
    )
    data = json.loads(r.choices[0].message.content)
    return data["subject"], data["body"]


async def send_draft_to_telegram(event, gmail, telegram_user_id, to_email, name, topic, sendername):
    """Generate the email text, save it as a Gmail draft, and show it to the
    user in Telegram with Send/Cancel buttons."""
    subject, body = await generate_email_text(topic, name, sendername)
    draft_id = create_gmail_draft(gmail, to_email, subject, body)
    draft_cache[draft_id] = telegram_user_id  # remember who this draft belongs to, for the button callback
    await event.reply(
        f"📧 Черновик для {name} ({to_email})\n\nТема: {subject}\n\n{body}",
        buttons=[Button.inline("✅ Отправить", f"send:{draft_id}"),
                 Button.inline("❌ Отмена", f"cancel:{draft_id}")],
    )


async def try_handle_email_intent(event, user_id: int, user_message: str) -> bool:
    """
    Entry point for "does this message want me to send an email?".
    Returns True if the message was handled as an email request (whatever the
    outcome - draft sent, asked for auth, asked for the recipient's email...),
    False if it should fall through to the normal chat flow.
    """
    # Ask the model once: is this an email request, and if so, with what details?
    r = await ai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": user_message}],
        tools=EMAIL_TOOLS, tool_choice="auto",
    )
    msg = r.choices[0].message
    if not msg.tool_calls:
        return False  # not an email request

    gmail = get_gmail_client_for(user_id)
    if not gmail:
        # User needs to (re)connect Gmail before we can do anything
        auth_url = build_auth_url(user_id)
        await event.reply(f"Сначала подключи (или переподключи) Gmail: {auth_url}")
        return True

    args = json.loads(msg.tool_calls[0].function.arguments)
    name = args["recipient_name"]
    topic = args["topic"]
    sendername = args.get("sender_name") or "Имя отсутствует, просто отправь без учета имени"

    try:
        # Prefer an email address the user gave explicitly, else search Gmail history
        email = args.get("recipient_email") or find_email_in_history(gmail, name)

        if not email:
            user_states[user_id] = ("waiting_for_manual_email", name, topic, sendername)
            await event.reply(f"Не нашёл email для {name}. Пришли его почту одним сообщением.")
            return True

        await send_draft_to_telegram(event, gmail, user_id, email, name, topic, sendername)
    except RefreshError:
        # Gmail access was revoked/expired mid-flow
        delete_user_token(user_id)
        auth_url = build_auth_url(user_id)
        await event.reply(
            "⚠️ Твой доступ к Gmail протух или был отозван.\n"
            f"Пожалуйста, авторизуйся заново по ссылке: {auth_url}"
        )
    return True


@client.on(events.CallbackQuery)
async def on_callback(event):
    """Handles the ✅ Отправить / ❌ Отмена buttons under a draft preview."""
    action, draft_id = event.data.decode().split(":")
    telegram_user_id = draft_cache.get(draft_id)
    gmail = get_gmail_client_for(telegram_user_id) if telegram_user_id else None

    if not gmail:
        auth_url = build_auth_url(event.sender_id)
        await event.edit(
            "❌ Ошибка: Сессия Gmail не найдена или истёк токен.\n\n"
            f"Авторизуйся заново по ссылке: {auth_url}"
        )
        return

    try:
        if action == "send":
            gmail.users().drafts().send(userId="me", body={"id": draft_id}).execute()
            await event.edit("✅ Отправлено")
        else:
            gmail.users().drafts().delete(userId="me", id=draft_id).execute()
            await event.edit("❌ Отменено")
    except RefreshError:
        delete_user_token(telegram_user_id)
        auth_url = build_auth_url(telegram_user_id)
        await event.edit(
            "⚠️ Токен Gmail был отозван или протух.\n\n"
            f"Пройди авторизацию заново по ссылке: {auth_url}"
        )
    except Exception as e:
        await event.edit(f"Ошибка при выполнении действия: {e}")
    finally:
        draft_cache.pop(draft_id, None)


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
    user_id = event.sender_id
    sender = await event.get_sender()

    if event.text and event.text.startswith("/"):
        return

    # Waiting for the user to manually type a recipient's email address
    state = user_states.get(user_id)
    if isinstance(state, tuple) and state[0] == "waiting_for_manual_email":
        _, name, topic, sendername = state
        email_match = re.search(r"[\w.+-]+@[\w.-]+", event.text or "")
        if email_match:
            gmail = get_gmail_client_for(user_id)
            del user_states[user_id]
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
            await event.reply("Это не похоже на email, попробуй ещё раз")
        return

    elif event.text and event.text.startswith("!"):
        if user_id in user_states:
            if user_states[user_id] == "waiting_for_submitmessage":
                message_tosend = event.text.split("/")
                await client.send_message(message_tosend[0][1:], message_tosend[1])

    else:
        if event.message.voice:
            user_states[user_id] = "waiting_for_voiceback"
            audio_path = "voice.mp3"
            try:
                voice_bytes = await event.message.download_media(file=bytes)
                if not voice_bytes:
                    await event.reply("Could not download voice message")
                    return
                audio_file = io.BytesIO(voice_bytes)
                audio_file.name = "voice.ogg"
                response = await ai_client.audio.transcriptions.create(
                    model="gpt-4o-mini-transcribe", file=audio_file
                )
                if response.text.strip():
                    pending_textvoice[user_id] = response.text
                    try:
                        handled = await try_handle_email_intent(event, user_id, pending_textvoice[user_id])
                        if not handled:
                            response = ask_gpt(chat_text=pending_textvoice[user_id])
                            await event.respond(response)
                    except Exception as e:
                        await event.respond(f"Sorry, I could not process your voice text: {e}")
                else:
                    await event.reply("Я тебя не понял бро")
            finally:
                if os.path.exists(audio_path):
                    os.remove(audio_path)

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
                    del user_states[user_id]
                    del pending_photos[user_id]
                return
        else:
            user_message = event.text
            if not user_message:
                return
            try:
                handled = await try_handle_email_intent(event, user_id, user_message)
                if not handled:
                    response = ask_gpt(chat_text=user_message)
                    await event.respond(response)
                    save_communication(sender.username if sender else None, user_message)
            except Exception as e:
                await event.respond(f"Не получилось обработать сообщение. Ошибка: {e}")