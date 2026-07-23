import asyncio
from telethon import TelegramClient, events, Button
from openai import AsyncOpenAI
import datetime
import os, io, re, json, base64
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from env import API_ID, API_HASH, BOT_TOKEN, OPENAI_API
from llm import ask_gpt, process_telegram_image
from database import SessionLocal, engine
import models
from fastapi import FastAPI

app = FastAPI()

client = TelegramClient('session_bot_korean', API_ID, API_HASH)
ai_client = AsyncOpenAI(api_key=OPENAI_API)
creds = Credentials.from_authorized_user_file("token.json")
gmail = build("gmail", "v1", credentials=creds)

user_states = {}
pending_photos = {}
pending_textvoice = {}
draft_cache = {}

# ── startup / shutdown ────────────────────────────────────────────
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

# ── health check ──────────────────────────────────────────────────
@app.get("/")
async def ping():
    return {"status": "ok"}

# ── db helpers (each function opens and closes its own session) ───
def save_communication(username, usermessage):
    db = SessionLocal()
    try:
        db.add(models.Communication(
            username=username,
            usermessage=usermessage
        ))
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

# email draft tool
EMAIL_TOOLS = [{
    "type": "function",
    "function": {
        "name": "create_email_draft",
        "description": "Подготовить черновик письма, когда пользователь просит написать/составить email кому-либо",
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_name": {"type": "string", "description": "Имя получателя латиницей, стандартный вид (напр. 'Mark Silverlock')"},
                "topic": {"type": "string", "description": "О чём письмо"},
            },
            "required": ["recipient_name", "topic"],
        },
    },
}]

def find_email(name: str) -> str | None:
    res = gmail.users().messages().list(userId="me", q=f'"{name}"', maxResults=5).execute()
    for m in res.get("messages", []):
        msg = gmail.users().messages().get(userId="me", id=m["id"], format="metadata",
                                            metadataHeaders=["From", "To"]).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        for v in (headers.get("From", ""), headers.get("To", "")):
            match = re.search(r"[\w.+-]+@[\w.-]+", v)
            if match and name.split()[0].lower() in v.lower():
                return match.group(0)
    return None

def create_gmail_draft(to: str, subject: str, body: str) -> str:
    raw = base64.urlsafe_b64encode(
        f"To: {to}\r\nSubject: {subject}\r\n\r\n{body}".encode()
    ).decode()
    d = gmail.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return d["id"]

async def generate_email_text(topic: str, recipient: str) -> tuple[str, str]:
    r = await ai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content":
            f"Напиши email на английском, получатель: {recipient}. Тема сообщения: {topic}. "
            f"Ответь строго JSON: {{\"subject\": \"...\", \"body\": \"...\"}}"}],
        response_format={"type": "json_object"},
    )
    data = json.loads(r.choices[0].message.content)
    return data["subject"], data["body"]

async def try_handle_email_intent(event, user_message: str) -> bool:
    """Возвращает True если это была задача на письмо и мы её обработали."""
    r = await ai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": user_message}],
        tools=EMAIL_TOOLS,
        tool_choice="auto",
    )
    msg = r.choices[0].message
    if not msg.tool_calls:
        return False
 
    args = json.loads(msg.tool_calls[0].function.arguments)
    name, topic = args["recipient_name"], args["topic"]
 
    email = find_email(name)
    if not email:
        await event.reply(f"Не нашёл email для {name}. Пришли его вручную?")
        return True
 
    subject, body = await generate_email_text(topic, name)
    draft_id = create_gmail_draft(email, subject, body)
    draft_cache[draft_id] = email
 
    await event.reply(
        f"📧 Черновик для {name} ({email})\n\nТема: {subject}\n\n{body}",
        buttons=[Button.inline("✅ Отправить", f"send:{draft_id}"),
                 Button.inline("❌ Отмена", f"cancel:{draft_id}")],
    )
    return True

@client.on(events.CallbackQuery)
async def on_callback(event):
    action, draft_id = event.data.decode().split(":")
    if action == "send":
        gmail.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        await event.edit("✅ Отправлено")
    else:
        gmail.users().drafts().delete(userId="me", id=draft_id).execute()
        await event.edit("❌ Отменено")
    draft_cache.pop(draft_id, None)

# ── telegram handlers ─────────────────────────────────────────────
@client.on(events.NewMessage(pattern='/start'))
async def start_message(event):
    await event.respond(
        'Привет! Меня зовут Кристина, я твой полноценный ассистент, проси меня о чем угодно!'
    )
    
@client.on(events.NewMessage(pattern='/sendShak'))
async def sendmessage(event):
    user_id = event.sender_id
    user_states[user_id] = "waiting_for_submitmessage"
    await event.respond(
        'send your message to shak'
    )

@client.on(events.NewMessage)
async def necessary_task_handler(event):
    user_id = event.sender_id
    sender = await event.get_sender()
 
    if event.text.startswith("/"):
        return
    
    elif event.text.startswith("!"):
        if user_id in user_states:
            if user_states[user_id] == "waiting_for_submitmessage":
                message_tosend = event.text.split("/")
                await client.send_message(message_tosend[0][1:], message_tosend[1])
                
    else:
        #process voice message
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
                    model="gpt-4o-mini-transcribe",  # Экономная и быстрая модель (или "whisper-1")
                    file=audio_file
                )
                if response.text.strip():
                    pending_textvoice[user_id] = response.text
                    try:
                        # сначала проверяем, не задача ли это на письмо
                        handled = await try_handle_email_intent(event, pending_textvoice[user_id])
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
        
        #process image    
        if event.photo:
            pending_photos[user_id] = event.message
            user_states[user_id] = "waiting_for_imageprompt"
            await event.respond("Получила вашу фотку! Что я должна сделать?")
            return
        #пользователь прислал фото, и мы ждем промпт
        if user_id in user_states and user_states[user_id] == "waiting_for_imageprompt":
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
            try:
                # сначала проверяем, не задача ли это на письмо
                handled = await try_handle_email_intent(event, user_message)
                if not handled:
                    response = ask_gpt(chat_text=user_message)
                    await event.respond(response)
                    save_communication(sender.username, user_message)
            except Exception as e:
                await event.respond(f"Не получилось обработать сообщение. Ошибка: {e}")