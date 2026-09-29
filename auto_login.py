#!/usr/bin/env python3
"""
auto_login.py
--------------
Полностью автоматический логин на портал DGIST:
1. Открывает постоянный профиль Chromium (тот же browser_profile/, что и
   в refresh_session.py).
2. Сам вводит логин/пароль в форму.
3. Сам подключается к почте по IMAP, ждёт письмо с кодом подтверждения,
   вытаскивает код регуляркой.
4. Сам вводит код в форму на сайте.
5. После успешного логина сохраняет cookie в session_cookie.txt —
   так же, как это делал refresh_session.py.

!!! ТЕБЕ НУЖНО ЗАПОЛНИТЬ ПЛЕЙСХОЛДЕРЫ НИЖЕ (раздел CONFIG) !!!
Я не могу открыть реальную страницу логина портала (домен вне моего
доступа), поэтому селекторы полей формы и параметры письма с кодом —
это заглушки. Как их найти — см. README.md, раздел "Настройка auto_login.py".

Хранение секретов:
- Используй ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ (DGIST_USERNAME, DGIST_PASSWORD,
  EMAIL_ADDRESS, EMAIL_APP_PASSWORD), не пиши их в код.
- Для почты используй ПРИЛОЖЕНИЕ-ПАРОЛЬ (app password), а не основной
  пароль аккаунта — почти все провайдеры (Gmail, Outlook, Naver, Daum)
  требуют этого для IMAP, если на аккаунте включена 2FA. Основной пароль
  почты для IMAP обычно просто не примут.
"""

import os
import re
import sys
import time
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

# ========================= CONFIG (ЗАПОЛНИ ПОД СЕБЯ) =========================

PORTAL_LOGIN_URL = "https://my.dgist.ac.kr/com/portal/index.do?language=en"
TARGET_DOMAIN = "stuecm.dgist.ac.kr"
ECM_HOME_URL = "https://stuecm.dgist.ac.kr"
ECM_SSO_WAIT_MS = 3000

# --- Селекторы формы логина на сайте (найди через DevTools -> Inspect) ---
USERNAME_FIELD_SELECTOR = "input#loginID"
PASSWORD_FIELD_SELECTOR = "input#password"
LOGIN_SUBMIT_SELECTOR = "button[onclick='passwordLogin();']"

# --- Поле ввода кода подтверждения (появляется после отправки логина/пароля) ---
# Между паролем и полем кода выскакивает промежуточный попап с кнопкой
# подтверждения ("код отправлен на почту") — его тоже нужно закрыть.
ALERT_CONFIRM_SELECTOR = "button#alert_btn"            # кнопка "확인" в попапе
CODE_FIELD_SELECTOR = "input#code"
CODE_SUBMIT_SELECTOR = "button[onclick='ok();']"       # кнопка "인증"

# Элемент, видимый только после успешного логина
LOGGED_IN_MARKER_SELECTOR = "div.user-info span.name"

# --- Настройки почты (IMAP), учётные данные портала — дефолты для .env-режима.
# Для бота эти значения НЕ используются напрямую — см. login_for_telegram_user()
# в самом низу файла, туда всё передаётся явными аргументами.
IMAP_HOST = os.environ.get("EMAIL_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = 993
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
MAILBOX = "INBOX"

# Фильтры письма с кодом — сузь, чтобы не подхватить чужое письмо
CODE_EMAIL_FROM_CONTAINS = "no-reply@dgist.ac.kr"
CODE_EMAIL_SUBJECT_CONTAINS = "인증"          # ловит "2차 인증 코드" и похожие варианты темы

# Регулярка для кода в теле письма (поправь под реальный формат — 6 цифр, буквы+цифры и т.п.)
CODE_REGEX = r"\b(\d{6})\b"

# Сколько ждать письма и как часто проверять почту
EMAIL_POLL_TIMEOUT_SEC = 90
EMAIL_POLL_INTERVAL_SEC = 3

# --- Учётные данные портала (.env-режим) ---
DGIST_USERNAME = os.environ.get("DGIST_USERNAME", "")
DGIST_PASSWORD = os.environ.get("DGIST_PASSWORD", "")

PROFILE_DIR = os.path.join(os.path.dirname(__file__), "browser_profile")
COOKIE_OUT_FILE = os.path.join(os.path.dirname(__file__), "session_cookie.txt")

# ========================= ПОЧТА: ПОИСК КОДА =========================


def _decode_maybe(s):
    if s is None:
        return ""
    parts = decode_header(s)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="ignore"))
        else:
            out.append(text)
    return "".join(out)


def _get_email_body(msg) -> str:
    if msg.is_multipart():
        chunks = []
        for part in msg.walk():
            content_type = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if content_type in ("text/plain", "text/html") and "attachment" not in disp:
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    chunks.append(payload.decode(charset, errors="ignore"))
                except Exception:
                    continue
        return "\n".join(chunks)
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="ignore")
        except Exception:
            return str(msg.get_payload())


def fetch_verification_code(
    min_timestamp: float,
    email_address: str,
    email_app_password: str,
    imap_host: str = "imap.gmail.com",
) -> str:
    """
    Подключается по IMAP и ищет письмо с кодом, пришедшее НЕ РАНЬШЕ
    min_timestamp (обычно — момент клика по кнопке логина, минус небольшой
    буфер на рассинхрон часов).

    Специально не полагается на флаг \\Seen (непрочитано/прочитано) —
    если ты откроешь письмо руками, чтобы подсмотреть код, это никак
    не собьёт скрипт. Все fetch-и идут через BODY.PEEK, так что скрипт
    вообще не трогает статус прочитанности твоих писем.

    Все параметры почты передаются явно (а не читаются из os.environ),
    чтобы эту функцию можно было безопасно переиспользовать для РАЗНЫХ
    людей в одном процессе бота, не путая чужие почтовые ящики.
    """
    if not email_address or not email_app_password:
        raise RuntimeError("Не переданы email_address / email_app_password.")

    deadline = time.time() + EMAIL_POLL_TIMEOUT_SEC
    # IMAP SINCE понимает только дату (без времени) — берём сегодняшний день,
    # чтобы не тащить всю историю почты, а точный отсев по времени сделаем
    # в питоне через заголовок Date.
    since_date = time.strftime("%d-%b-%Y", time.gmtime(min_timestamp))

    while time.time() < deadline:
        M = imaplib.IMAP4_SSL(imap_host, IMAP_PORT)
        try:
            M.login(email_address, email_app_password)
            M.select(MAILBOX, readonly=True)  # readonly — доп. страховка не трогать флаги

            criteria = ["SINCE", since_date]
            if CODE_EMAIL_FROM_CONTAINS:
                criteria += ["FROM", f'"{CODE_EMAIL_FROM_CONTAINS}"']

            status, data = M.search(None, *criteria)
            candidates = []

            if status == "OK" and data and data[0]:
                ids = data[0].split()
                for msg_id in ids:
                    # Сначала берём только заголовки (PEEK — не трогает \Seen)
                    status, hdr_data = M.fetch(msg_id, "(BODY.PEEK[HEADER])")
                    if status != "OK" or not hdr_data or not hdr_data[0]:
                        continue
                    raw_header = hdr_data[0][1]
                    hdr_msg = email.message_from_bytes(raw_header)

                    from_ = _decode_maybe(hdr_msg.get("From"))
                    subject = _decode_maybe(hdr_msg.get("Subject"))
                    date_hdr = hdr_msg.get("Date")

                    if CODE_EMAIL_FROM_CONTAINS and CODE_EMAIL_FROM_CONTAINS not in from_:
                        continue
                    if CODE_EMAIL_SUBJECT_CONTAINS and CODE_EMAIL_SUBJECT_CONTAINS not in subject:
                        continue

                    try:
                        msg_dt = parsedate_to_datetime(date_hdr)
                        msg_ts = msg_dt.timestamp()
                    except Exception:
                        msg_ts = 0  # если дата не парсится — не отсекаем, но и не приоритизируем

                    # 15 сек буфера на рассинхрон часов между сервером и этой машиной
                    if msg_ts < min_timestamp - 15:
                        continue

                    candidates.append((msg_ts, msg_id))

            if candidates:
                # Берём самое свежее письмо из подходящих
                candidates.sort(key=lambda x: x[0], reverse=True)
                _, newest_id = candidates[0]

                status, body_data = M.fetch(newest_id, "(BODY.PEEK[])")
                if status == "OK" and body_data and body_data[0]:
                    raw_email = body_data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    body = _get_email_body(msg)
                    m = re.search(CODE_REGEX, body)
                    if m:
                        M.logout()
                        return m.group(1)
        finally:
            try:
                M.logout()
            except Exception:
                pass

        time.sleep(EMAIL_POLL_INTERVAL_SEC)

    raise TimeoutError(
        "Не дождался письма с кодом подтверждения за отведённое время. "
        "Проверь CODE_EMAIL_FROM_CONTAINS / CODE_EMAIL_SUBJECT_CONTAINS / CODE_REGEX."
    )


# ========================= БРАУЗЕР: ЛОГИН =========================


def cookies_to_header_string(cookies: list, domain_filter: str = None) -> str:
    # Для DGIST ECM нельзя ограничиваться только cookie stuecm:
    # SSO может установить cookie на isign/auth и затем использовать
    # её при переходе в ECM. Поэтому по умолчанию сохраняем ВСЕ cookie.
    parts = []
    for c in cookies:
        if domain_filter and domain_filter not in c.get("domain", ""):
            continue
        parts.append(f"{c['name']}={c['value']}")
    return "; ".join(parts)


def bootstrap_ecm_sso(page, context) -> str:
    """
    Открывает ECM настоящим Chromium после portal-login.

    Важный момент: stuecm может вернуть маленькую HTML-страницу,
    которая делает JS POST/submit на isign.dgist.ac.kr с agentId=31.
    requests этого JS не выполнит, а Playwright выполнит. После перехода
    собираем cookie со всех доменов, а не только stuecm.
    """
    print("[SSO] Открываю DGIST ECM, чтобы браузер выполнил SSO...")
    try:
        page.goto(ECM_HOME_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"[SSO] Первый переход завершился исключением: {e}")

    # Даём JS submit/redirect и установке cookie время завершиться.
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(ECM_SSO_WAIT_MS)

    print(f"[SSO] Финальный URL: {page.url}")
    print(f"[SSO] Title: {page.title()}")

    cookies = context.cookies()
    print(f"[SSO] Cookie count after ECM bootstrap: {len(cookies)}")
    for c in cookies:
        print(f"[SSO] cookie domain={c.get('domain')} name={c.get('name')}")

    # Если после bootstrap мы всё ещё на isign/login.html, SSO не завершился.
    if "isign.dgist.ac.kr/login.html" in page.url:
        raise RuntimeError(
            "ECM SSO не завершился: браузер остался на isign.dgist.ac.kr/login.html. "
            "Нужно исследовать дополнительный SSO-шаг/redirect в Chrome Network."
        )

    # Критически важно: передаём все cookie. Не только stuecm.
    return cookies_to_header_string(cookies)


def login_and_get_cookie(
    dgist_username: str,
    dgist_password: str,
    email_address: str,
    email_app_password: str,
    profile_dir: str,
    cookie_out_file: str,
    imap_host: str = "imap.gmail.com",
) -> str:
    """
    Вся логика логина в одной функции, без обращений к os.environ —
    всё приходит аргументами. Это то, что должен вызывать бот (через
    login_for_telegram_user() ниже), передавая КОНКРЕТНЫЕ credentials
    и КОНКРЕТНЫЕ пути для КОНКРЕТНОГО telegram_user_id.

    Возвращает строку cookie и попутно сохраняет её в cookie_out_file.
    """
    if not dgist_username or not dgist_password:
        raise RuntimeError("Не переданы dgist_username / dgist_password.")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile_dir, headless=True)
        page = context.pages[0] if context.pages else context.new_page()

        page.goto(PORTAL_LOGIN_URL, wait_until="networkidle")

        # Если уже залогинены (профиль сохранил живую сессию) — просто сохраняем cookie и выходим
        try:
            page.wait_for_selector(LOGGED_IN_MARKER_SELECTOR, timeout=4000)
            print("Уже залогинены (сессия из профиля жива), логин не требуется.")
            cookie_str = bootstrap_ecm_sso(page, context)
            with open(cookie_out_file, "w", encoding="utf-8") as f:
                f.write(cookie_str)
            context.close()
            return cookie_str
        except Exception:
            pass  # не залогинены — идём логиниться ниже

        print("Ввожу логин и пароль...")
        page.fill(USERNAME_FIELD_SELECTOR, dgist_username)
        page.fill(PASSWORD_FIELD_SELECTOR, dgist_password)
        login_click_time = time.time()  # с этого момента ищем письмо с кодом
        page.click(LOGIN_SUBMIT_SELECTOR)

        print("Закрываю промежуточный попап (код отправлен на почту)...")
        page.wait_for_selector(ALERT_CONFIRM_SELECTOR, timeout=15000)
        page.click(ALERT_CONFIRM_SELECTOR)

        print("Жду поле ввода кода подтверждения...")
        page.wait_for_selector(CODE_FIELD_SELECTOR, timeout=15000)

        print("Иду в почту за кодом (может занять до "
              f"{EMAIL_POLL_TIMEOUT_SEC} сек)...")
        code = fetch_verification_code(
            min_timestamp=login_click_time,
            email_address=email_address,
            email_app_password=email_app_password,
            imap_host=imap_host,
        )
        print(f"Код получен: {code}")

        page.fill(CODE_FIELD_SELECTOR, code)
        page.click(CODE_SUBMIT_SELECTOR)

        page.wait_for_selector(LOGGED_IN_MARKER_SELECTOR, timeout=15000)
        print("Логин подтверждён.")

        cookie_str = bootstrap_ecm_sso(page, context)
        with open(cookie_out_file, "w", encoding="utf-8") as f:
            f.write(cookie_str)
        print(f"ECM/SSO cookie сохранена в {cookie_out_file}.")

        context.close()
        return cookie_str


def login_for_telegram_user(
    telegram_user_id: int,
    dgist_username: str,
    dgist_password: str,
    email_address: str,
    email_app_password: str,
    imap_host: str = "imap.gmail.com",
) -> str:
    """
    ТОЧКА ВХОДА ДЛЯ ТЕЛЕГРАМ-БОТА (пара к dgist_monitor.run_once_for_user).

    telegram_user_id — ТОЛЬКО из update.effective_user.id на стороне бота.
    dgist_username/password/email_* — то, что бот достал из своей БД ПО
    ЭТОМУ telegram_user_id (не из какого-либо другого источника, не из
    кэша "последнего активного юзера").

    Профиль браузера и файл cookie автоматически кладутся в
    user_data/<telegram_user_id>/ — используется build_user_paths()
    из dgist_monitor.py, чтобы пути для логина и для мониторинга
    гарантированно совпадали.
    """
    from dgist_monitor import build_user_paths  # локальный импорт, чтобы избежать циклов

    paths = build_user_paths(telegram_user_id)
    return login_and_get_cookie(
        dgist_username=dgist_username,
        dgist_password=dgist_password,
        email_address=email_address,
        email_app_password=email_app_password,
        profile_dir=paths["browser_profile_dir"],
        cookie_out_file=paths["cookie_file"],
        imap_host=imap_host,
    )


def main():
    """CLI-режим для одного человека — берёт всё из .env, как раньше."""
    cookie = login_and_get_cookie(
        dgist_username=DGIST_USERNAME,
        dgist_password=DGIST_PASSWORD,
        email_address=EMAIL_ADDRESS,
        email_app_password=EMAIL_APP_PASSWORD,
        profile_dir=PROFILE_DIR,
        cookie_out_file=COOKIE_OUT_FILE,
        imap_host=IMAP_HOST,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ОШИБКА] {e}", file=sys.stderr)
        sys.exit(1)
