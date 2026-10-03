import os
from collections import defaultdict, deque
from dotenv import load_dotenv
from openai import AsyncOpenAI
import base64

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY not found in .env")

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

MAX_HISTORY_PER_USER = 10
user_contexts = defaultdict(lambda: deque(maxlen=MAX_HISTORY_PER_USER))


async def ask_gpt(chat_text: str, user_id: int | None = None) -> str:
    """Асинхронный диалог с GPT-4o-mini с изоляцией контекста по пользователям."""
    history = user_contexts[user_id] if user_id is not None else deque(maxlen=MAX_HISTORY_PER_USER)

    history.append({
        "role": "user",
        "content": chat_text
    })

    system_message = {
        "role": "system",
        "content": "You are a helpful assistant named Кристина. Respond clearly, concisely, and politely."
    }

    messages = [system_message] + list(history)

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.7
    )

    reply = response.choices[0].message.content or ""

    history.append({
        "role": "assistant",
        "content": reply
    })

    return reply


async def process_telegram_image(message, user_prompt: str) -> str:
    """Асинхронный анализ изображения через GPT-4o Vision."""
    image_bytes = await message.download_media(file=bytes)
    if not image_bytes:
        return "Не удалось загрузить изображение из сообщения."

    base64_image = base64.b64encode(image_bytes).decode("utf-8")

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
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

    return response.choices[0].message.content or "Ответ пуст."


async def gpt_stream(user_prompt: str, retrieved_context: str) -> str:
    """Анализ контекста базы знаний."""
    system_prompt = f"""Ты профессиональный консультант, который помогает находить ответ на вопрос. Отвечай ТОЛЬКО на основе предоставленного контекста. 
Если информации недостаточно - вежливо откажись отвечать. Сохраняй дружелюбный и уверенный тон.

Правила:
1. Анализируй контекст из базы знаний: <CONTEXT_START> {retrieved_context} </CONTEXT_END>.
2. Отвечай максимально конкретно без воды в пределах одного-трех предложений на вопрос: {user_prompt}
3. Не используй фразы «насколько я знаю», «по моим данным», «вероятно», «скорее всего» — только факты или признание отсутствия фактов.
4. Пиши ОЧЕНЬ кратко, чётко, по делу. Избегай воды.
5. Используй markdown для оформления: заголовки, списки, выделение **важного**, `кода`, > цитат.
6. Отвечай на языке вопроса пользователя.
"""

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system_prompt}],
        temperature=0.7
    )
    return response.choices[0].message.content or ""
