import asyncio
from telethon import TelegramClient, events
import datetime

from env import API_ID, API_HASH, BOT_TOKEN
from database import SessionLocal, engine
import models
from fastapi import FastAPI

app = FastAPI()

client = TelegramClient('session_bot_korean', API_ID, API_HASH)

user_states = {}

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
def add_task(user_id, task, date, time, importance):
    db = SessionLocal()
    try:
        db.add(models.Todolist(
            user_id=user_id,
            todo=task,
            dead_date=date,
            dead_time=time,
            importance=importance
        ))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"add_task error: {e}")
        raise
    finally:
        db.close()

def add_available_time(user_id, date, start, end):
    db = SessionLocal()
    try:
        db.add(models.Availabletime(
            user_id=user_id,
            date=date,
            free_time_start=start,
            free_time_end=end
        ))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"add_available_time error: {e}")
        raise
    finally:
        db.close()

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

def get_availabletimes_fortoday(user_id):
    db = SessionLocal()
    try:
        freetime_start = db.query(models.Availabletime.free_time_start).where(
            models.Availabletime.user_id == user_id,
            models.Availabletime.date == get_current_datetime()[0]
        ).all()
        freetime_end = db.query(models.Availabletime.free_time_end).where(
            models.Availabletime.user_id == user_id,
            models.Availabletime.date == get_current_datetime()[0]
        ).all()
        return [freetime_start, freetime_end]
    finally:
        db.close()

# ── telegram handlers ─────────────────────────────────────────────
@client.on(events.NewMessage(pattern='/start'))
async def start_message(event):
    await event.respond(
        'HIIII! I can help you organize your day.\nTap /help to see what I can do.'
    )

@client.on(events.NewMessage(pattern='/help'))
async def help_message(event):
    await event.respond(
        'Here are the commands you can use:\n\n'
        '/start - Start the bot\n'
        '/help - Show this help message\n'
        '/addtask - Add a new task\n'
        '/get_todolist - Get your current to-do list\n'
        '/markdone - Mark a task as done\n'
        '/deletetask - Delete a task\n'
        '/add_available_time - Add your available time slots\n'
        '/todolist_fortoday - Get your to-do list for today'
    )

@client.on(events.NewMessage(pattern='/addtask'))
async def addtask(event):
    user_id = event.sender_id
    user_states[user_id] = "waiting_for_task"
    await event.respond(
        'To add your task, send it in this format:\n'
        '!task name/2026-03-23/11:00/5\n\n'
        'example:\n'
        '!Quiz: data structure/2026-03-23/11:00/5'
    )
    
@client.on(events.NewMessage(pattern='/sendShak'))
async def sendmessage(event):
    user_id = event.sender_id
    user_states[user_id] = "waiting_for_submitmessage"
    await event.respond(
        'send your message to shak'
    )

@client.on(events.NewMessage(pattern="/add_available_time"))
async def addavailabletime(event):
    user_id = event.sender_id
    user_states[user_id] = "waiting_for_availabletime"
    await event.respond(
        'To add your available times, send it in this format:\n'
        '!date/freetime_start/freetime_end\n\n'
        'example:\n'
        '!2026-04-29/13:00/15:00\n'
        'Call /add_available_time again to add more slots.'
    )

@client.on(events.NewMessage)
async def necessary_task_handler(event):
    user_id = event.sender_id
    sender = await event.get_sender()

    if event.text.startswith("/"):
        return
    elif event.text.startswith("!"):
        if user_id in user_states:
            if user_states[user_id] == "waiting_for_task":
                task_details = event.text.split("/")
                try:
                    add_task(user_id, task_details[0][1:], task_details[1], task_details[2], task_details[3])
                    await event.respond("Successfully saved your task")
                except:
                    await event.respond("Could not save task into database, try again")
            elif user_states[user_id] == "waiting_for_availabletime":
                availability_details = event.text.split("/")
                try:
                    add_available_time(user_id, availability_details[0][1:], availability_details[1], availability_details[2])
                    await event.respond("Successfully saved your available time")
                except:
                    await event.respond("Could not save available times into database, try again")
            elif user_states[user_id] == "waiting_for_submitmessage":
                message_tosend = event.text.split("/")
                await client.send_message(message_tosend[0][1:], message_tosend[1])
    else:
        user_message = event.text
        try:
            from llm import ask_gpt
            response = ask_gpt(chat_text=user_message)
            await event.respond(response)
            save_communication(sender.username, user_message)
        except Exception as e:
            await event.respond(f"Sorry, I could not process your message: {e}")
