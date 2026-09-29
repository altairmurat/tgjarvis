"""
dgist_bot_handlers.py
-----------------------
ЧТО ТУТ:
1. Команда /connectmyportal — пошагово (4 сообщения) спрашивает логин,
   пароль, email для кода и app password, сохраняет в БД (зашифрованно).
2. Команда /checkportal — логинится на портал под данными юзера
   и присылает дайджест последних 10 постов + новые важные.
3. Команда /disconnectportal — удаляет сохранённые креды.
4. Обработка ответов в рамках диалога — ЭТИ elif-БЛОКИ НУЖНО ВСТАВИТЬ
   в твой существующий necessary_task_handler (см. инструкцию в конце файла,
   я специально пометил комментарием [ВСТАВИТЬ СЮДА]).

ЗАВИСИМОСТИ:
- dgist_monitor.py и auto_login.py (уже готовый проект мониторинга DGIST)
  должны лежать в том же каталоге, что и этот бот, ЛИБО быть доступны
  через PYTHONPATH.
- pip install cryptography
- models.py должен содержать класс DgistAccount (см. models_addition.py)
- .env должен содержать SECRETS_ENCRYPTION_KEY (см. crypto_utils.py)

ВАЖНЫЙ МОМЕНТ ПРО ASYNCIO НА WINDOWS:
Playwright (auto_login.py) запускается НЕ напрямую, а через отдельный
процесс auto_login_cli.py (см. _run_login_subprocess ниже). Это из-за
конфликта между Telethon (нужен SelectorEventLoop на Windows) и Playwright
(нужен ProactorEventLoop для subprocess). Полностью независимый процесс
избегает этого конфликта — у него своя чистая политика event loop.

dgist_monitor.py (requests + BeautifulSoup, без Playwright) конфликта не
имеет и вызывается напрямую через asyncio.to_thread(...) как обычно.
"""

import asyncio
import os
import subprocess
import sys
import traceback
from telethon import events

import dgist_monitor
from dgist_accounts import (
    save_dgist_account,
    load_dgist_account,
    delete_dgist_account,
    list_dgist_user_ids,
)

# Чтобы не запускать несколько параллельных проверок для одного и того же
# юзера (например, если он дважды подряд ткнёт /checkportal, пока первая
# ещё крутится) — простой in-memory "замок" по telegram_user_id.
_active_portal_checks: set[int] = set()

# Автоматическая проверка портала каждые 5 часов.
AUTO_CHECK_INTERVAL_SEC = 5 * 60

# Фоновая задача автопроверки.
_auto_monitor_task = None


def register_dgist_handlers(client, user_states: dict):
    """
    Регистрирует DGIST-команды и запускает фоновый монитор.

    После /connectmyportal пользователь автоматически проверяется
    каждые 5 часов. /checkportal остаётся как ручная проверка "прямо сейчас".
    """

    global _auto_monitor_task

    @client.on(events.NewMessage(pattern='/connectmyportal'))
    async def connect_portal(event):
        user_id = event.sender_id
        user_states[user_id] = "waiting_for_dgist_username"
        await event.respond(
            "🎓 Подключаем DGIST-портал.\n\n"
            "1/4 Введи свой логин (ID) от портала DGIST:"
        )

    @client.on(events.NewMessage(pattern='/disconnectportal'))
    async def disconnect_portal(event):
        user_id = event.sender_id
        await asyncio.to_thread(delete_dgist_account, user_id)
        user_states.pop(user_id, None)
        await event.respond("🗑 Данные портала удалены из базы.")

    @client.on(events.NewMessage(pattern='/cancel'))
    async def cancel_flow(event):
        if event.sender_id in user_states:
            del user_states[event.sender_id]
            await event.respond("Ок, отменил текущий диалог.")

    @client.on(events.NewMessage(pattern='/checkportal'))
    async def check_portal(event):
        user_id = event.sender_id
        account = await asyncio.to_thread(load_dgist_account, user_id)

        if not account:
            await event.respond(
                "Портал ещё не подключён. Сначала: /connectmyportal"
            )
            return

        if user_id in _active_portal_checks:
            await event.respond("Уже идёт проверка этого портала, подожди немного...")
            return

        _active_portal_checks.add(user_id)
        status_msg = await event.respond("🔄 Проверяю DGIST...")

        try:
            result = await _check_user_portal(user_id, account)

            if result.get("error"):
                await status_msg.edit(
                    f"❌ Не удалось проверить портал:\n{result['error']}"
                )
            else:
                message = _format_new_posts_message(result)
                await status_msg.edit(message)

        except Exception as e:
            print(f"[/checkportal error] user_id={user_id}")
            traceback.print_exc()
            await status_msg.edit(
                f"❌ Не получилось проверить портал: {str(e) or repr(e)}"
            )
        finally:
            _active_portal_checks.discard(user_id)

    # Запускаем один фоновый цикл на весь процесс бота.
    if _auto_monitor_task is None or _auto_monitor_task.done():
        _auto_monitor_task = asyncio.create_task(
            _auto_monitor_loop(client)
        )

    return {
        "connect_portal": connect_portal,
        "disconnect_portal": disconnect_portal,
        "cancel_flow": cancel_flow,
        "check_portal": check_portal,
    }


async def _check_user_portal(user_id: int, account: dict) -> dict:
    """
    Проверяет портал с существующей cookie.

    ВАЖНО:
    - Не логинимся через браузер каждые 5 часов.
    - Сначала используем уже сохранённую cookie.
    - Если cookie протухла/отсутствует, только тогда запускаем auto_login.
    - После нового логина повторяем проверку один раз.
    """
    paths = dgist_monitor.build_user_paths(user_id)
    cookie_file = paths["cookie_file"]

    cookie = ""
    if os.path.exists(cookie_file):
        with open(cookie_file, "r", encoding="utf-8") as f:
            cookie = f.read().strip()

    # Первый обычный запрос.
    if cookie:
        result = await asyncio.to_thread(
            dgist_monitor.run_once_for_user, user_id, cookie
        )

        if result and not _result_has_session_error(result):
            return result

        print(f"[AUTO] Cookie пользователя {user_id} протухла. Перелогиниваюсь...")

    # Cookie отсутствует или сессия протухла.
    await asyncio.to_thread(_run_login_subprocess, user_id, account)

    if not os.path.exists(cookie_file):
        raise RuntimeError("auto_login завершился, но session_cookie.txt не найден.")

    with open(cookie_file, "r", encoding="utf-8") as f:
        cookie = f.read().strip()

    if not cookie:
        raise RuntimeError("auto_login создал пустой session_cookie.txt.")

    # Повторяем проверку уже с новой SSO-сессией.
    result = await asyncio.to_thread(
        dgist_monitor.run_once_for_user, user_id, cookie
    )

    if result is None:
        return {
            "new_important": [],
            "new_minor": [],
            "digest_top10": [],
            "error": "Монитор вернул пустой результат после обновления сессии.",
        }

    return result


def _result_has_session_error(result: dict) -> bool:
    """Понимает как новый, так и старый формат результата монитора."""
    if not isinstance(result, dict):
        return True

    error = str(result.get("error", "")).lower()

    if not error:
        return False

    markers = (
        "сессия",
        "session",
        "cookie",
        "login",
        "isign",
        "stuecm",
    )
    return any(marker in error for marker in markers)


def _format_new_posts_message(result: dict) -> str:
    """
    Сообщение БЕЗ Top-10.

    Отправляем только реально новые посты.
    И важные, и обычные — пользователь просил получать каждый новый пост.
    """
    if not result:
        return "❌ Монитор вернул пустой результат."

    if result.get("error"):
        return f"❌ {result['error']}"

    posts = list(result.get("new_important", []))
    posts += list(result.get("new_minor", []))

    if not posts:
        return "✅ Проверка завершена. Новых объявлений нет."

    lines = ["🔔 Новые объявления DGIST:\n"]

    for e in posts:
        emoji = "🔴" if e.get("important") else "📢"
        category = e.get("category", "DGIST")
        title = e.get("title", "Без названия")
        date = e.get("date", "")
        summary = e.get("summary", "").strip()

        lines.append(f"{emoji} [{category}]")
        lines.append(f"{title}")
        if date:
            lines.append(f"📅 {date}")
        if summary:
            lines.append(summary)
        lines.append("")

    # Telegram ограничивает размер одного сообщения.
    text = "\n".join(lines).strip()
    return text[:3900]


async def _auto_monitor_loop(client):
    """
    Главный фоновой цикл.

    Проверяет всех пользователей, у которых есть сохранённый
    DGIST-аккаунт:
        1. сразу после запуска бота;
        2. затем каждые 5 часов.

    Ничего не пишет в Telegram, если новых постов нет.
    """
    await asyncio.sleep(5)

    while True:
        try:
            user_ids = await asyncio.to_thread(list_dgist_user_ids)

            print(
                f"[AUTO] Начинаю проверку DGIST для "
                f"{len(user_ids)} подключённых пользователей."
            )

            for user_id in user_ids:
                if user_id in _active_portal_checks:
                    print(f"[AUTO] user={user_id}: уже проверяется, пропускаю.")
                    continue

                account = await asyncio.to_thread(
                    load_dgist_account, user_id
                )
                if not account:
                    continue

                _active_portal_checks.add(user_id)

                try:
                    paths = dgist_monitor.build_user_paths(user_id)

                    # Если state-файла ещё нет, это первый автоматический запуск.
                    # Монитор сам создаст baseline. Результат не отправляем,
                    # чтобы не получить старые объявления пачкой.
                    first_run = not os.path.exists(paths["state_file"])

                    result = await _check_user_portal(user_id, account)

                    if result.get("error"):
                        print(
                            f"[AUTO] user={user_id}: "
                            f"{result['error']}"
                        )
                    elif not first_run:
                        posts = list(result.get("new_important", []))
                        posts += list(result.get("new_minor", []))

                        if posts:
                            message = _format_new_posts_message(result)

                            try:
                                await client.send_message(
                                    user_id,
                                    message,
                                    link_preview=False,
                                )
                                print(
                                    f"[AUTO] user={user_id}: "
                                    f"отправлено новых постов: {len(posts)}"
                                )
                            except Exception:
                                print(
                                    f"[AUTO] Не удалось отправить Telegram "
                                    f"сообщение user={user_id}"
                                )
                                traceback.print_exc()
                        else:
                            print(
                                f"[AUTO] user={user_id}: "
                                f"новых постов нет."
                            )
                    else:
                        print(
                            f"[AUTO] user={user_id}: "
                            f"создан baseline, старые посты не отправляем."
                        )

                except Exception:
                    print(f"[AUTO] Ошибка проверки user={user_id}")
                    traceback.print_exc()
                finally:
                    _active_portal_checks.discard(user_id)

            print(
                f"[AUTO] Следующая проверка через "
                f"{AUTO_CHECK_INTERVAL_SEC // 3600} часов."
            )

        except Exception:
            print("[AUTO] Ошибка фонового цикла:")
            traceback.print_exc()

        await asyncio.sleep(AUTO_CHECK_INTERVAL_SEC)

def _run_login_subprocess(telegram_user_id: int, account: dict) -> None:
    """
    Запускает auto_login_cli.py как ОТДЕЛЬНЫЙ процесс через синхронный
    subprocess.run (не asyncio-версию!) — именно синхронность тут и
    решает конфликт event loop policy между Telethon и Playwright на
    Windows. Эта функция сама по себе блокирующая, поэтому вызывающий
    код всегда оборачивает её в asyncio.to_thread(...).

    Креды передаются через env, а не argv — чтобы не светились в
    списке процессов (Task Manager / ps).
    """
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_login_cli.py")

    env = os.environ.copy()
    env["CLI_DGIST_USERNAME"] = account["dgist_username"]
    env["CLI_DGIST_PASSWORD"] = account["dgist_password"]
    env["CLI_EMAIL_ADDRESS"] = account["email_address"]
    env["CLI_EMAIL_APP_PASSWORD"] = account["email_app_password"]
    env["CLI_EMAIL_IMAP_HOST"] = account["email_imap_host"]

    proc = subprocess.run(
        [sys.executable, script_path, str(telegram_user_id)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()
        # оставляем только хвост — там обычно самое интересное (наш "ERROR: ...")
        raise RuntimeError(f"логин не удался: {tail[-500:]}")


def _format_digest_message(result: dict) -> str:
    lines = []

    if result["new_important"]:
        lines.append("🔴 Новые важные объявления:\n")
        for e in result["new_important"]:
            lines.append(f"[{e['category']}] {e['title']} ({e['date']})")
            lines.append(e["summary"])
            lines.append("")  # пустая строка-разделитель

    if not result["new_important"] and not result["new_minor"]:
        lines.append("Новых объявлений с прошлой проверки нет.\n")

    lines.append("📋 Топ-10 последних постов по всем разделам:")
    for p in result["digest_top10"]:
        lines.append(f"[{p['category']}] {p['date']} — {p['title']}")

    return "\n".join(lines)


async def handle_dgist_conversation_step(event, user_id: int, state, user_states: dict) -> bool:
    """
    [ВСТАВИТЬ СЮДА] — вызови эту функцию ПЕРВОЙ строкой внутри своего
    necessary_task_handler, сразу после строки:

        state = user_states.get(user_id)

    вот так:

        state = user_states.get(user_id)
        if await handle_dgist_conversation_step(event, user_id, state, user_states):
            return
        if isinstance(state, tuple) and state[0] == "waiting_for_manual_email":
            ...

    Функция возвращает True, если она обработала сообщение (и тогда твой
    handler должен сразу сделать return, не пытаясь обработать это же
    сообщение как обычный чат/email-интент), либо False, если это
    сообщение её не касается — тогда твой handler продолжает работать как раньше.
    """
    from dgist_accounts import save_dgist_account  # локальный импорт, чтобы избежать циклов

    text = (event.text or "").strip()

    if state == "waiting_for_dgist_username":
        if not text:
            await event.reply("Логин не может быть пустым. Введи его ещё раз:")
            return True
        user_states[user_id] = ("waiting_for_dgist_password", text)
        await event.respond(
            "2/4 Теперь пароль от портала DGIST.\n\n"
            "⚠️ Сообщение с паролем удалю из чата сразу после получения."
        )
        return True

    if isinstance(state, tuple) and state[0] == "waiting_for_dgist_password":
        _, dgist_username = state
        try:
            await event.delete()
        except Exception:
            pass
        if not text:
            await event.reply("Пароль не может быть пустым. Введи его ещё раз:")
            return True
        user_states[user_id] = ("waiting_for_dgist_email", dgist_username, text)
        await event.respond(
            "3/4 На какой email приходит код подтверждения (2FA) с портала?\n"
            "(та почта, куда реально падает письмо с кодом)"
        )
        return True

    if isinstance(state, tuple) and state[0] == "waiting_for_dgist_email":
        _, dgist_username, dgist_password = state
        if "@" not in text:
            await event.reply("Это не похоже на email. Введи ещё раз:")
            return True
        user_states[user_id] = ("waiting_for_dgist_email_app_password", dgist_username, dgist_password, text)
        await event.respond(
            "4/4 Теперь App Password от этой почты (НЕ обычный пароль от аккаунта!).\n\n"
            "Как получить (Gmail): myaccount.google.com → Security → App passwords.\n"
            "⚠️ Это сообщение тоже удалю сразу после получения."
        )
        return True

    if isinstance(state, tuple) and state[0] == "waiting_for_dgist_email_app_password":
        _, dgist_username, dgist_password, email_address = state
        try:
            await event.delete()
        except Exception:
            pass
        if not text:
            await event.reply("App password не может быть пустым. Введи его ещё раз:")
            return True

        await asyncio.to_thread(
            save_dgist_account,
            user_id, dgist_username, dgist_password, email_address, text,
        )
        del user_states[user_id]
        await event.respond(
            "✅ Готово! Данные сохранены (пароли зашифрованы в БД).\n\n"
            "Команда /checkportal проверит объявления."
        )
        return True

    return False