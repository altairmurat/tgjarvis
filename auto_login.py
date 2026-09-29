#!/usr/bin/env python3

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


# ============================================================
# CONFIG
# ============================================================

PORTAL_LOGIN_URL = (
    "https://my.dgist.ac.kr/com/portal/index.do?language=en"
)

# После обычного логина обязательно открываем ECM.
ECM_URL = (
    "https://stuecm.dgist.ac.kr/site/ecmSiteList/index.do"
)

USERNAME_FIELD_SELECTOR = "input#loginID"
PASSWORD_FIELD_SELECTOR = "input#password"
LOGIN_SUBMIT_SELECTOR = "button[onclick='passwordLogin();']"

ALERT_CONFIRM_SELECTOR = "button#alert_btn"

CODE_FIELD_SELECTOR = "input#code"
CODE_SUBMIT_SELECTOR = "button[onclick='ok();']"

LOGGED_IN_MARKER_SELECTOR = "div.user-info span.name"


IMAP_HOST = os.environ.get(
    "EMAIL_IMAP_HOST",
    "imap.gmail.com"
)

IMAP_PORT = 993

EMAIL_ADDRESS = os.environ.get(
    "EMAIL_ADDRESS",
    ""
)

EMAIL_APP_PASSWORD = os.environ.get(
    "EMAIL_APP_PASSWORD",
    ""
)

MAILBOX = "INBOX"

CODE_EMAIL_FROM_CONTAINS = "no-reply@dgist.ac.kr"
CODE_EMAIL_SUBJECT_CONTAINS = "인증"

CODE_REGEX = r"\b(\d{6})\b"

EMAIL_POLL_TIMEOUT_SEC = 90
EMAIL_POLL_INTERVAL_SEC = 3

DGIST_USERNAME = os.environ.get(
    "DGIST_USERNAME",
    ""
)

DGIST_PASSWORD = os.environ.get(
    "DGIST_PASSWORD",
    "")

PROFILE_DIR = os.path.join(
    os.path.dirname(__file__),
    "browser_profile"
)

COOKIE_OUT_FILE = os.path.join(
    os.path.dirname(__file__),
    "session_cookie.txt"
)


# ============================================================
# EMAIL
# ============================================================

def _decode_maybe(value):
    if value is None:
        return ""

    parts = decode_header(value)
    result = []

    for text, encoding in parts:
        if isinstance(text, bytes):
            result.append(
                text.decode(
                    encoding or "utf-8",
                    errors="ignore"
                )
            )
        else:
            result.append(text)

    return "".join(result)


def _get_email_body(msg) -> str:

    if msg.is_multipart():

        chunks = []

        for part in msg.walk():

            content_type = part.get_content_type()

            disposition = str(
                part.get("Content-Disposition", "")
            )

            if (
                content_type in ("text/plain", "text/html")
                and "attachment" not in disposition
            ):

                try:

                    payload = part.get_payload(
                        decode=True
                    )

                    charset = (
                        part.get_content_charset()
                        or "utf-8"
                    )

                    chunks.append(
                        payload.decode(
                            charset,
                            errors="ignore"
                        )
                    )

                except Exception:
                    continue

        return "\n".join(chunks)

    try:

        payload = msg.get_payload(
            decode=True
        )

        charset = (
            msg.get_content_charset()
            or "utf-8"
        )

        return payload.decode(
            charset,
            errors="ignore"
        )

    except Exception:

        return str(
            msg.get_payload()
        )


def fetch_verification_code(
    min_timestamp: float,
    email_address: str,
    email_app_password: str,
    imap_host: str = "imap.gmail.com",
) -> str:

    if not email_address or not email_app_password:
        raise RuntimeError(
            "Не переданы email_address / email_app_password."
        )

    deadline = (
        time.time()
        + EMAIL_POLL_TIMEOUT_SEC
    )

    since_date = time.strftime(
        "%d-%b-%Y",
        time.gmtime(min_timestamp)
    )

    while time.time() < deadline:

        M = imaplib.IMAP4_SSL(
            imap_host,
            IMAP_PORT
        )

        try:

            M.login(
                email_address,
                email_app_password
            )

            M.select(
                MAILBOX,
                readonly=True
            )

            criteria = [
                "SINCE",
                since_date
            ]

            if CODE_EMAIL_FROM_CONTAINS:

                criteria += [
                    "FROM",
                    f'"{CODE_EMAIL_FROM_CONTAINS}"'
                ]

            status, data = M.search(
                None,
                *criteria
            )

            candidates = []

            if (
                status == "OK"
                and data
                and data[0]
            ):

                ids = data[0].split()

                for msg_id in ids:

                    status, hdr_data = M.fetch(
                        msg_id,
                        "(BODY.PEEK[HEADER])"
                    )

                    if (
                        status != "OK"
                        or not hdr_data
                        or not hdr_data[0]
                    ):
                        continue

                    raw_header = hdr_data[0][1]

                    hdr_msg = email.message_from_bytes(
                        raw_header
                    )

                    from_ = _decode_maybe(
                        hdr_msg.get("From")
                    )

                    subject = _decode_maybe(
                        hdr_msg.get("Subject")
                    )

                    date_hdr = hdr_msg.get(
                        "Date"
                    )

                    if (
                        CODE_EMAIL_FROM_CONTAINS
                        and CODE_EMAIL_FROM_CONTAINS
                        not in from_
                    ):
                        continue

                    if (
                        CODE_EMAIL_SUBJECT_CONTAINS
                        and CODE_EMAIL_SUBJECT_CONTAINS
                        not in subject
                    ):
                        continue

                    try:

                        msg_dt = (
                            parsedate_to_datetime(
                                date_hdr
                            )
                        )

                        msg_ts = msg_dt.timestamp()

                    except Exception:

                        msg_ts = 0

                    if (
                        msg_ts
                        < min_timestamp - 15
                    ):
                        continue

                    candidates.append(
                        (
                            msg_ts,
                            msg_id
                        )
                    )

            if candidates:

                candidates.sort(
                    key=lambda x: x[0],
                    reverse=True
                )

                _, newest_id = candidates[0]

                status, body_data = M.fetch(
                    newest_id,
                    "(BODY.PEEK[])"
                )

                if (
                    status == "OK"
                    and body_data
                    and body_data[0]
                ):

                    raw_email = body_data[0][1]

                    msg = email.message_from_bytes(
                        raw_email
                    )

                    body = _get_email_body(
                        msg
                    )

                    match = re.search(
                        CODE_REGEX,
                        body
                    )

                    if match:

                        M.logout()

                        return match.group(1)

        finally:

            try:
                M.logout()
            except Exception:
                pass

        time.sleep(
            EMAIL_POLL_INTERVAL_SEC
        )

    raise TimeoutError(
        "Не дождался письма с кодом подтверждения."
    )


# ============================================================
# COOKIES
# ============================================================

def cookies_to_header_string(cookies):

    """
    Берём cookies ВСЕХ DGIST-доменов.

    Это важно, потому что SSO использует:
        auth.dgist.ac.kr
        my.dgist.ac.kr
        isign.dgist.ac.kr
        www.dgist.ac.kr
        stuecm.dgist.ac.kr
    """

    allowed_domains = (
        "dgist.ac.kr",
    )

    parts = []

    for cookie in cookies:

        domain = cookie.get(
            "domain",
            ""
        )

        if not any(
            allowed in domain
            for allowed in allowed_domains
        ):
            continue

        name = cookie.get(
            "name"
        )

        value = cookie.get(
            "value"
        )

        if name and value:

            parts.append(
                f"{name}={value}"
            )

    return "; ".join(parts)


def save_cookies(
    context,
    cookie_out_file
):

    cookies = context.cookies()

    print(
        f"[SSO] Cookie count: {len(cookies)}"
    )

    for cookie in cookies:

        print(
            "[SSO] "
            f"cookie domain={cookie.get('domain')} "
            f"name={cookie.get('name')}"
        )

    cookie_str = cookies_to_header_string(
        cookies
    )

    if not cookie_str:

        raise RuntimeError(
            "После SSO не удалось получить DGIST cookies."
        )

    os.makedirs(
        os.path.dirname(
            cookie_out_file
        ),
        exist_ok=True
    )

    with open(
        cookie_out_file,
        "w",
        encoding="utf-8"
    ) as f:

        f.write(cookie_str)

    print(
        f"[SSO] Cookie сохранена в "
        f"{cookie_out_file}"
    )

    print(
        f"[SSO] Cookie length: "
        f"{len(cookie_str)}"
    )

    return cookie_str


# ============================================================
# LOGIN
# ============================================================

def login_and_get_cookie(
    dgist_username: str,
    dgist_password: str,
    email_address: str,
    email_app_password: str,
    profile_dir: str,
    cookie_out_file: str,
    imap_host: str = "imap.gmail.com",
) -> str:

    if not dgist_username:
        raise RuntimeError(
            "Не передан DGIST username."
        )

    if not dgist_password:
        raise RuntimeError(
            "Не передан DGIST password."
        )

    print(
        "[SSO] Запускаю Chromium..."
    )

    with sync_playwright() as p:

        context = (
            p.chromium.launch_persistent_context(
                profile_dir,
                headless=True
            )
        )

        try:

            page = (
                context.pages[0]
                if context.pages
                else context.new_page()
            )

            # ------------------------------------------------
            # 1. Открываем портал
            # ------------------------------------------------

            print(
                "[SSO] Открываю DGIST portal..."
            )

            page.goto(
                PORTAL_LOGIN_URL,
                wait_until="networkidle",
                timeout=60000
            )

            # ------------------------------------------------
            # 2. Проверяем существующую сессию
            # ------------------------------------------------

            try:

                page.wait_for_selector(
                    LOGGED_IN_MARKER_SELECTOR,
                    timeout=4000
                )

                print(
                    "[SSO] Существующая сессия жива."
                )

            except Exception:

                # ------------------------------------------------
                # 3. Новый логин
                # ------------------------------------------------

                print(
                    "[SSO] Ввожу логин и пароль..."
                )

                page.fill(
                    USERNAME_FIELD_SELECTOR,
                    dgist_username
                )

                page.fill(
                    PASSWORD_FIELD_SELECTOR,
                    dgist_password
                )

                login_click_time = time.time()

                page.click(
                    LOGIN_SUBMIT_SELECTOR
                )

                # ------------------------------------------------
                # 4. Popup
                # ------------------------------------------------

                print(
                    "[SSO] Жду popup..."
                )

                page.wait_for_selector(
                    ALERT_CONFIRM_SELECTOR,
                    timeout=15000
                )

                page.click(
                    ALERT_CONFIRM_SELECTOR
                )

                # ------------------------------------------------
                # 5. Код
                # ------------------------------------------------

                print(
                    "[SSO] Жду поле кода..."
                )

                page.wait_for_selector(
                    CODE_FIELD_SELECTOR,
                    timeout=15000
                )

                print(
                    "[SSO] Получаю код из почты..."
                )

                code = fetch_verification_code(
                    min_timestamp=login_click_time,
                    email_address=email_address,
                    email_app_password=email_app_password,
                    imap_host=imap_host,
                )

                print(
                    f"[SSO] Код получен: {code}"
                )

                page.fill(
                    CODE_FIELD_SELECTOR,
                    code
                )

                page.click(
                    CODE_SUBMIT_SELECTOR
                )

                # ------------------------------------------------
                # 6. Ждём подтверждение логина
                # ------------------------------------------------

                print(
                    "[SSO] Жду завершения SSO..."
                )

                page.wait_for_selector(
                    LOGGED_IN_MARKER_SELECTOR,
                    timeout=30000
                )

                print(
                    "[SSO] Логин подтверждён."
                )

            # ------------------------------------------------
            # 7. Открываем ECM
            # ------------------------------------------------

            print(
                "[SSO] Открываю DGIST ECM..."
            )

            page.goto(
                ECM_URL,
                wait_until="networkidle",
                timeout=60000
            )

            print(
                "[SSO] Финальный URL: "
                f"{page.url}"
            )

            print(
                "[SSO] Title: "
                f"{page.title()}"
            )

            # ------------------------------------------------
            # 8. Проверяем, что ECM реально открылся
            # ------------------------------------------------

            if "isign.dgist.ac.kr/login" in page.url.lower():

                raise RuntimeError(
                    "ECM отправил обратно на isign login. "
                    "SSO не завершился."
                )

            if "stuecm.dgist.ac.kr" not in page.url:

                raise RuntimeError(
                    "Не удалось открыть DGIST ECM. "
                    f"Текущий URL: {page.url}"
                )

            print(
                "[SSO] ECM успешно открыт."
            )

            # ------------------------------------------------
            # 9. Теперь сохраняем ВСЕ cookies
            # ------------------------------------------------

            cookie_str = save_cookies(
                context,
                cookie_out_file
            )

            print(
                "[SSO] Авторизация полностью завершена."
            )

            return cookie_str

        finally:

            try:
                context.close()
            except Exception:
                pass


# ============================================================
# TELEGRAM USER
# ============================================================

def login_for_telegram_user(
    telegram_user_id: int,
    dgist_username: str,
    dgist_password: str,
    email_address: str,
    email_app_password: str,
    imap_host: str = "imap.gmail.com",
) -> str:

    from dgist_monitor import build_user_paths

    paths = build_user_paths(
        telegram_user_id
    )

    return login_and_get_cookie(
        dgist_username=dgist_username,
        dgist_password=dgist_password,
        email_address=email_address,
        email_app_password=email_app_password,
        profile_dir=paths[
            "browser_profile_dir"
        ],
        cookie_out_file=paths[
            "cookie_file"
        ],
        imap_host=imap_host,
    )


# ============================================================
# CLI
# ============================================================

def main():

    login_and_get_cookie(
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

        print(
            f"[ОШИБКА] {e}",
            file=sys.stderr
        )

        sys.exit(1)