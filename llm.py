import os
from dotenv import load_dotenv
from openai import OpenAI
from io import BytesIO
import base64

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
        
    
    prompt = f"""You are helpful assistant with name Кристина.
Conversation:
{chat_text}

Context:
{chat_context}

Respond clearly and try to be short most of the time. 
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

async def process_telegram_image(message):
    # 1. Скачиваем изображение в память (BytesIO)
    image_buffer = BytesIO()
    await message.download_media(file=image_buffer)
    
    # 2. Получаем сырые байты и кодируем в base64
    image_bytes = image_buffer.getvalue()
    base64_image = base64.b64encode(image_bytes).decode("utf-8")

    # 3. Отправляем в OpenAI
    response = client.chat.completions.create( # В актуальной версии API используется этот метод
        model="gpt-4o", # Убедитесь, что используете модель с поддержкой зрения (напр. gpt-4o)
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Что на этом изображении?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}"
                        },
                    },
                ],
            }
        ],
    )

    return response.choices[0].message.content