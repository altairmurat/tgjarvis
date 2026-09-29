#!/usr/bin/env python3
"""
auto_login_cli.py
-------------------
Запускает auto_login.login_for_telegram_user() в ОТДЕЛЬНОМ процессе.

ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ:
На Windows у Telethon и Playwright несовместимые требования к политике
asyncio event loop:
- Telethon исторически требует SelectorEventLoop (у ProactorEventLoop
  были баги с SSL в некоторых версиях asyncio/aiohttp на Windows).
- Playwright для запуска браузера ОБЯЗАТЕЛЬНО требует ProactorEventLoop,
  потому что только он на Windows умеет создавать subprocess через asyncio.

Если вызывать Playwright прямо в процессе бота (даже через
asyncio.to_thread) — новый event loop внутри потока наследует ГЛОБАЛЬНУЮ
политику процесса (Selector, из-за Telethon), и Playwright падает с
NotImplementedError при попытке запустить браузер.

Решение: этот файл запускается как СОВЕРШЕННО ОТДЕЛЬНЫЙ процесс через
обычный (синхронный, без asyncio) subprocess.run(). У него своя чистая
Python-среда со своей политикой event loop по умолчанию (Proactor на
Windows), никак не связанная с тем, что творится в процессе бота.

Учётные данные передаются через переменные окружения (не через argv),
чтобы пароли не светились в списке процессов (Task Manager/ps).
"""

import os
import sys

import auto_login


def main():
    if len(sys.argv) < 2:
        print("ERROR: не передан telegram_user_id первым аргументом", file=sys.stderr)
        sys.exit(1)

    telegram_user_id = int(sys.argv[1])

    dgist_username = os.environ.get("CLI_DGIST_USERNAME", "")
    dgist_password = os.environ.get("CLI_DGIST_PASSWORD", "")
    email_address = os.environ.get("CLI_EMAIL_ADDRESS", "")
    email_app_password = os.environ.get("CLI_EMAIL_APP_PASSWORD", "")
    imap_host = os.environ.get("CLI_EMAIL_IMAP_HOST", "imap.gmail.com")

    if not all([dgist_username, dgist_password, email_address, email_app_password]):
        print("ERROR: не переданы все необходимые CLI_* переменные окружения", file=sys.stderr)
        sys.exit(1)

    auto_login.login_for_telegram_user(
        telegram_user_id=telegram_user_id,
        dgist_username=dgist_username,
        dgist_password=dgist_password,
        email_address=email_address,
        email_app_password=email_app_password,
        imap_host=imap_host,
    )
    # cookie уже сохранена внутри login_for_telegram_user в файл —
    # родительскому процессу (боту) достаточно знать, что мы вышли с кодом 0
    print("OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)