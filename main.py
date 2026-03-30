import asyncio
from telethon import TelegramClient, events
import datetime

from env import API_ID, API_HASH, BOT_TOKEN

from database import SessionLocal, engine
import models

from fastapi import FastAPI

app = FastAPI()


client = TelegramClient('session_bot_korean', API_ID, API_HASH) #.start(bot_token=BOT_TOKEN)
#client.start()

@app.on_event("startup")
async def startup_event():
    await client.start(bot_token=BOT_TOKEN)
    asyncio.create_task(client.run_until_disconnected())
    
@app.on_event("shutdown")
async def shutdown_event():
    await client.disconnect()

@app.get("/")
async def ping():
    return {"status": "ok"}

models.Base.metadata.create_all(bind=engine)

db = SessionLocal()

user_states = {}

@client.on(events.NewMessage(pattern='/start'))
async def start_message(event):
    await event.respond(f'''HIIII! I can help you organize your day. 
                        \nTap /help to see what I can do.''')
    
@client.on(events.NewMessage(pattern='/help'))
async def help_message(event):
    await event.respond(f'''Here are the commands you can use:
                        \n\n/start - Start the bot
                        \n/help - Show this help message
                        \n/addtask - Add a new task
                        \n/get_todolist - Get your current to-do list
                        \n/markdone - Mark a task as done
                        \n/deletetask - Delete a task
                        \n/add_available_time - Add your available time slots
                        \n/todolist_fortoday - Get your to-do list for today''')

#addtask
def add_task(user_id, task, date, time, importance):
    try:
        db.add(models.Todolist(
            user_id=user_id,
            todo = task,
            dead_date = date,
            dead_time = time,
            importance = importance
        ))
        db.commit()
    finally:
        db.close()
        
@client.on(events.NewMessage(pattern='/addtask'))
async def addtask(event):
    user_id = event.sender_id
    user_states[user_id] = "waiting_for_task"
    
    await event.respond(f'''To add your task to to-do-list, you must send it in the following format:
                        \n!task name/deadline date: 2026-03-23/deadline time: 11:00/importance: any number from 1 to 5
                        \n\nexample:
                        \n!Quiz: data structure/2026-03-23/11:00/5''')
    
#get_todolist

#markdone

#deletetask

#add_available_time

def add_available_time(user_id, date, start, end):
    try:
        db.add(models.Availabletime(
            user_id = user_id,
            date = date,
            free_time_start = start,
            free_time_end = end
        ))
        db.commit()
    finally:
        db.close()
        
@client.on(events.NewMessage(pattern="/add_available_time"))
async def addavailabletime(event):
    user_id = event.sender_id
    user_states[user_id] = "waiting_for_availabletime"
    
    await event.respond(f'''To add your available times to memory, you must send it in the following format:
                        \n!date: 2026-03-23/freetime_start: 11:00/freetime_end: 13:00
                        \n\nexample:
                        \n!2026-04-29/13:00/15:00
                        \nif you wish to add other available time slots, call the function /add_available_time again
                        ''')

#todolist_fortoday

def get_current_datetime():
    current_datetime = str(datetime.datetime.now()).split(" ") #2026-03-23 22:27:15.123456
    current_date, current_time = current_datetime[0], current_datetime[1][:5]
    return [current_date, current_time]

def get_availabletimes_fortoday(user_id):
    freetime_start = db.query(models.Availabletime.free_time_start).where(models.Availabletime.user_id == user_id, models.Availabletime.date == get_current_datetime()[0]).all()
    freetime_end = db.query(models.Availabletime.free_time_end).where(models.Availabletime.user_id == user_id, models.Availabletime.date == get_current_datetime()[0]).all()
    
    return [freetime_start, freetime_end] #list of all rows from the column// [[('13:00',), ('10:00',)], [('15:00',), ('12:00',)]]

def create_table_fortoday(user_id):
    tasks = db.query(models.Todolist.todo).where(models.Todolist.user_id == user_id).order_by(models.Todolist.importance.desc()).all()
    tasks[0]
    
#save communication

def save_communication(username, usermessage):
    try:
        db.add(models.Communication(
            username = username,
            usermessage = usermessage
        ))
        db.commit()
    finally:
        db.close()

#main messages handler
@client.on(events.NewMessage)
async def necessary_task_handler(event):
    user_id = event.sender_id
    sender = await event.get_sender()
    
    if event.text.startswith("/"): #ignore texts that are commands
        return
    elif event.text.startswith("!"): #recognize adding something
        if user_id in user_states:
            if user_states[user_id] == "waiting_for_task": #if state is "add_task"
                task_details = event.text.split("/")
                try:
                    add_task(user_id, task_details[0][1:], task_details[1], task_details[2], task_details[3])
                    await event.respond("Successfully saved your task")
                except:
                    await event.respond("Could not save task into database, try again")
            elif user_states[user_id] == "waiting_for_availabletime": #if state is "add_availabletime"
                availability_details = event.text.split("/")
                try:
                    add_available_time(user_id, availability_details[0][1:], availability_details[1], availability_details[2])
                    await event.respond("Successfully saved your available time")
                except:
                    await event.respond("Could not save available times into database, try again")
    else:
        user_message = event.text
        from llmollama import ollama_stream
        response = ollama_stream(user_prompt=user_message)
        await client.send_message(sender.username, response)
        save_communication(sender.username, user_message)
        

#start a bot
#with client:
#    client.start()
#client.run_until_disconnected()

#async def main():
#    await client.start(bot_token=BOT_TOKEN)
#    await client.run_until_disconnected()

#if __name__ == '__main__':
#    asyncio.run(main())