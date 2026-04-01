import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY not found in .env")

client = OpenAI(api_key=OPENAI_API_KEY)

#function to ask GPT for analysis

chat_context = []

def ask_gpt(chat_text: str) -> str:
    
    chat_context.append({
        "role": "user",
        "content": chat_text
    })
    
    prompt = f"""You are helpful assistant with name Jarvis.
Conversation:
{chat_text}

Context:
{chat_context}

Respond clearly, VERYVERY VERY ООООООЧЕНЬ shortly. Общайся как нормальный человек. 
"""
    response = client.chat.completions.create(
        model="gpt-4o-mini", 
        messages=[{
            "role": "user", 
            "content": prompt
            }, *chat_context], 
        temperature=0.7)
    
    reply = response.choices[0].message.content
    
    chat_context.append({
        "role": "assistant",
        "content": reply
    })
    
    return reply
