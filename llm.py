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

async def process_telegram_image(message, user_prompt):
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
                    {"type": "text", "text": f"{user_prompt}"},
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



def gpt_stream(user_prompt: str, retrieved_context: str):
    system_prompt = f"""Ты профессиональный консультант который помогает находить ответ на любой вопрос. Отвечай ТОЛЬКО на основе предоставленного контекста. 
    Если информации недостаточно - вежливо откажись отвечать. Сохраняй дружелюбный и уверенный тон.
        
        Правила (очень важные, следуй строго по порядку важности):

        1. Анализируй контекст из базы знании: <CONTEXT_START> {retrieved_context} </CONTEXT_END>.
        2. Отвечай максимально конкретно без воды в пределах одного-трех предложении из базы знании контекста на вопрос: {user_prompt}
        3. Не используй фразы «насколько я знаю», «по моим данным», «вероятно», «скорее всего» — только факты или признание отсутствия фактов.
        4. 
        5. 
        6. Пиши ОЧЕНЬ кратко, чётко, по делу. Избегай воды.
        7. Используй markdown для оформления: заголовки, списки, таблицы, выделение **важного**, `кода`, > цитат.
        8. Отвечай на языке вопроса пользователя. 
        
    Пример, как тебе нужно отвечать
    ВОПРОС: - Сколько стоит цена продукта?
    ТВОЙ ОТВЕТ: - Цена продукта ТОО Ромашка равна 5000 долларов.
    """
    
    response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": system_prompt}], temperature=0.7)
    return response.choices[0].message.content
