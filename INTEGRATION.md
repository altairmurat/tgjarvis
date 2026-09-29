# Интеграция DGIST-мониторинга в твоего бота

## Шаг 0. Файлы, которые должны лежать рядом

В той же папке, где твой основной файл бота, должны быть:

```
dgist_monitor.py          <- из прошлого проекта (мониторинг доски)
auto_login.py             <- из прошлого проекта (автологин + 2FA по почте)
crypto_utils.py           <- новый (этот чат)
dgist_accounts.py         <- новый (этот чат)
dgist_bot_handlers.py     <- новый (этот чат)
```

## Шаг 1. models.py

Вставь класс `DgistAccount` из `models_addition.py` в конец своего
`models.py` (после класса `Communication`). Ничего руками мигрировать
не нужно — таблица создастся сама при следующем запуске, у тебя уже
есть `models.Base.metadata.create_all(bind=engine)` в `startup_event`.

## Шаг 2. .env / env.py

Сгенерируй ключ шифрования один раз:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Положи результат в `.env`:
```
SECRETS_ENCRYPTION_KEY=вставь_сюда_ключ
```

И добавь в свой `env.py` (там же, где `API_ID`, `BOT_TOKEN` и т.д.):
```python
SECRETS_ENCRYPTION_KEY = os.environ["SECRETS_ENCRYPTION_KEY"]
```

(crypto_utils.py читает эту переменную напрямую из os.environ, отдельно
импортировать в основной файл её не обязательно — но пусть будет в env.py
для единообразия с остальными секретами.)

## Шаг 3. Установка зависимостей

```bash
pip install cryptography
```

(playwright/requests/bs4/dotenv у тебя уже должны быть от прошлого проекта)

## Шаг 4. Импорты в основном файле бота

В самый верх, рядом с остальными импортами:

```python
import auto_login
import dgist_monitor
from dgist_bot_handlers import register_dgist_handlers, handle_dgist_conversation_step
```

## Шаг 5. Регистрация хендлеров при старте

В `startup_event`, ПОСЛЕ `await client.start(bot_token=BOT_TOKEN)`:

```python
@app.on_event("startup")
async def startup_event():
    try:
        models.Base.metadata.create_all(bind=engine)
        print("Database tables created successfully")
    except Exception as e:
        print(f"DB init error: {e}")
    await client.start(bot_token=BOT_TOKEN)
    register_dgist_handlers(client, user_states)   # <-- ВСТАВИТЬ ЭТУ СТРОКУ
    asyncio.create_task(client.run_until_disconnected())
```

## Шаг 6. Единственная правка внутри necessary_task_handler

Найди в своём коде:

```python
@client.on(events.NewMessage)
async def necessary_task_handler(event):
    user_id = event.sender_id
    sender = await event.get_sender()

    if event.text and event.text.startswith("/"):
        return

    state = user_states.get(user_id)
    if isinstance(state, tuple) and state[0] == "waiting_for_manual_email":
```

И вставь ОДНУ строку сразу после `state = user_states.get(user_id)`,
ДО существующей проверки `waiting_for_manual_email`:

```python
    state = user_states.get(user_id)

    if await handle_dgist_conversation_step(event, user_id, state):   # <-- ВСТАВИТЬ
        return                                                          # <-- ВСТАВИТЬ

    if isinstance(state, tuple) and state[0] == "waiting_for_manual_email":
        ...
```

Это всё. `handle_dgist_conversation_step` сама разбирается, её ли это
сообщение (по состоянию `waiting_for_dgist_*`) — если нет, возвращает
`False` и твой остальной код работает как работал.

## Проверка, что всё подключилось

1. Перезапусти бота.
2. В Telegram: `/connectmyportal` → пройди 4 шага (логин, пароль, email, app password).
   Сообщения с паролем и app password бот удалит сам сразу после прочтения.
3. `/checkportal` → бот залогинится на портал (может занять ~30-90 сек
   из-за ожидания письма с кодом при первом разе) и пришлёт дайджест.
4. При повторных `/checkportal` (если сессия из профиля браузера ещё
   жива) должно быть намного быстрее — без похода в почту.

## На что обратить внимание

- **`/cancel`** — если юзер застрял посреди диалога подключения
  (например, случайно закрыл чат), эта команда сбрасывает `user_states`.
- **Одновременные проверки одного юзера** блокируются через
  `_active_portal_checks` в `dgist_bot_handlers.py` — второй `/checkportal`
  от того же человека, пока первый ещё крутится, не запустит второй Chromium.
- **Много юзеров одновременно** — сейчас каждый `/checkportal` синхронно
  ждёт (через `asyncio.to_thread`) свой Chromium/scraping. Если ботом
  пользуются десятки человек одновременно, стоит подумать про очередь
  задач (Celery/RQ) вместо прямого вызова из хендлера — но для старта
  и умеренной нагрузки текущего варианта достаточно.
- **`user_data/<telegram_user_id>/`** — папка появится рядом с
  `dgist_monitor.py`/`auto_login.py` и будет расти на каждого подключённого
  юзера (профиль браузера может весить десятки МБ). Не забудь, что она
  должна быть в `.gitignore`, если используешь git.
