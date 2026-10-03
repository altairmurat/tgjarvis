#!/usr/bin/env python3
"""
DGIST Bulletin Board Monitor
-----------------------------

Текущая схема:

1. auto_login.py отдельно логинит пользователя через браузер/SSO.
2. Cookie конкретного пользователя сохраняется в:
       user_data/<telegram_user_id>/session_cookie.txt
3. Этот модуль использует эту cookie для проверки DGIST.
4. При первом запуске существующие объявления просто записываются
   в seen_ids.json и НЕ отправляются пользователю.
5. При следующих проверках обрабатываются только новые объявления.
6. Для новых объявлений Gemini делает короткое описание.
7. Если Gemini недоступен / quota 429 — используем обычный preview
   текста объявления вместо падения всего мониторинга.
"""

import os
import re
import json
import time
import requests

from bs4 import BeautifulSoup
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://stuecm.dgist.ac.kr"

CATEGORIES = {
    "_0031": ("PB_ST00220031", "Latest / Student Bulletin"),
    "_0014": ("PB_ST00220014", "Academic Notice"),
    "_0161": ("BD000240", "Scholarships"),
    "_0162": ("PB_ST00220162", "Career & Jobs"),
    "_0004": ("PB_ST00220004", "Student Support"),
}

SITE_KEY = "ST0022"
LANGUAGE = "en"

# Сколько записей брать со страницы каждого раздела.
# Это НЕ означает, что мы будем отправлять 10 старых постов.
# Нужны только последние записи, чтобы обнаружить новые.
POSTS_PER_CATEGORY = 20

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATE_FILE = os.path.join(
    BASE_DIR,
    "seen_ids.json",
)

LOG_FILE = os.path.join(
    BASE_DIR,
    "summaries_log.jsonl",
)

SESSION_COOKIE_FILE = os.path.join(
    BASE_DIR,
    "session_cookie.txt",
)

REQUEST_DELAY_SEC = 1.0


# ============================================================
# USER PATHS
# ============================================================

def build_user_paths(telegram_user_id: int) -> dict:
    """
    Все данные конкретного Telegram-пользователя хранятся отдельно.
    """

    base = os.path.join(
        BASE_DIR,
        "user_data",
        str(telegram_user_id),
    )

    os.makedirs(base, exist_ok=True)

    return {
        "state_file": os.path.join(
            base,
            "seen_ids.json",
        ),

        "log_file": os.path.join(
            base,
            "summaries_log.jsonl",
        ),

        "cookie_file": os.path.join(
            base,
            "session_cookie.txt",
        ),

        "browser_profile_dir": os.path.join(
            base,
            "browser_profile",
        ),
    }


# ============================================================
# GEMINI
# ============================================================

GEMINI_API_KEY = os.environ.get(
    "GEMINI_API_KEY",
    "",
)

GEMINI_MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite",
)


# ============================================================
# HTTP
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": BASE_URL,
}


# ============================================================
# EXCEPTIONS
# ============================================================

class SessionExpiredError(Exception):
    pass


# ============================================================
# COOKIE
# ============================================================

def _load_session_cookie() -> str:
    """
    Однопользовательский fallback.
    Для Telegram используется build_user_paths().
    """

    if os.path.exists(SESSION_COOKIE_FILE):
        try:
            with open(
                SESSION_COOKIE_FILE,
                "r",
                encoding="utf-8",
            ) as f:
                value = f.read().strip()

            if value:
                return value

        except Exception:
            pass

    return os.environ.get(
        "DGIST_COOKIE",
        "",
    )


SESSION_COOKIE = _load_session_cookie()


def load_user_cookie(telegram_user_id: int) -> str:
    """
    Загружает cookie конкретного Telegram-пользователя.

    Основной путь:
        user_data/<telegram_id>/session_cookie.txt

    Дополнительно поддерживает старую схему через DGIST_COOKIE.
    """

    paths = build_user_paths(telegram_user_id)

    cookie_file = paths["cookie_file"]

    if os.path.exists(cookie_file):

        try:
            with open(
                cookie_file,
                "r",
                encoding="utf-8",
            ) as f:
                cookie = f.read().strip()

            if cookie:
                print(
                    f"[COOKIE] Загружена cookie пользователя "
                    f"{telegram_user_id}"
                )

                return cookie

        except Exception as e:
            print(
                f"[COOKIE] Ошибка чтения {cookie_file}: {e}"
            )

    if SESSION_COOKIE:
        return SESSION_COOKIE

    raise RuntimeError(
        f"Не найдена cookie для пользователя "
        f"{telegram_user_id}."
    )


# ============================================================
# SESSION
# ============================================================

def build_session(cookie: str = None) -> requests.Session:

    cookie = (
        cookie
        if cookie is not None
        else SESSION_COOKIE
    )

    if not cookie:
        raise RuntimeError(
            "Не найдена cookie сессии. "
            "Сначала выполни авторизацию через auto_login.py."
        )

    session = requests.Session()

    session.headers.update(
        HEADERS
    )

    # auto_login сохраняет все DGIST cookies
    # одним Cookie header.
    session.headers["Cookie"] = cookie

    return session


# ============================================================
# LOGIN PAGE DETECTION
# ============================================================

def looks_like_login_page(
    html: str,
    response_url: str = "",
) -> bool:

    html = html or ""

    lowered = html.lower()

    url = (
        response_url or ""
    ).lower()

    login_url_markers = (
        "isign.dgist.ac.kr/login.html",
        "/login.html",
        "/login.do",
        "/sso/login",
    )

    if any(
        marker in url
        for marker in login_url_markers
    ):
        return True

    strong_markers = (
        'name="loginform"',
        'id="loginform"',
        'name="login_form"',
        'id="login_form"',
        'name="userpassword"',
        'name="j_password"',
        'id="userpassword"',
        'id="password"',
    )

    strong_count = sum(
        marker in lowered
        for marker in strong_markers
    )

    if strong_count >= 2:
        return True

    # DGIST SSO response вида:
    #
    # var sendUrl = "https://isign.dgist.ac.kr/login.html";
    #
    if (
        "sendurl" in lowered
        and
        "isign.dgist.ac.kr/login.html" in lowered
        and
        "agentid" in lowered
    ):
        return True

    return False


# ============================================================
# STATE
# ============================================================

def load_seen(
    state_file: str = None,
) -> dict:

    state_file = (
        state_file
        or STATE_FILE
    )

    if not os.path.exists(state_file):
        return {}

    try:
        with open(
            state_file,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception as e:
        print(
            f"[STATE] Не удалось прочитать "
            f"{state_file}: {e}"
        )

    return {}


def save_seen(
    seen: dict,
    state_file: str = None,
) -> None:

    state_file = (
        state_file
        or STATE_FILE
    )

    directory = os.path.dirname(
        state_file
    )

    if directory:
        os.makedirs(
            directory,
            exist_ok=True,
        )

    tmp_file = state_file + ".tmp"

    with open(
        tmp_file,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            seen,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        tmp_file,
        state_file,
    )


# ============================================================
# LOG
# ============================================================

def append_log(
    entry: dict,
    log_file: str = None,
) -> None:

    log_file = (
        log_file
        or LOG_FILE
    )

    directory = os.path.dirname(
        log_file
    )

    if directory:
        os.makedirs(
            directory,
            exist_ok=True,
        )

    with open(
        log_file,
        "a",
        encoding="utf-8",
    ) as f:

        f.write(
            json.dumps(
                entry,
                ensure_ascii=False,
            )
            + "\n"
        )


# ============================================================
# TEXT PREVIEW
# ============================================================

def make_preview(
    text: str,
    max_len: int = 300,
) -> str:

    text = " ".join(
        (text or "").split()
    )

    if not text:
        return ""

    if len(text) <= max_len:
        return text

    return (
        text[:max_len]
        .rstrip()
        + "…"
    )


# ============================================================
# FETCH LIST
# ============================================================

def fetch_list(
    session: requests.Session,
    menu_key: str,
    board_id: str,
    page: int = 1,
):

    url = (
        f"{BASE_URL}"
        f"/s/potal_board/"
        f"{menu_key}/listBbs.do"
    )

    body = {
        "currentPage": page,
        "BOARD_ID": board_id,
        "SITE_KEY": SITE_KEY,
        "MENU_KEY": menu_key,
        "language": LANGUAGE,
    }

    resp = session.post(
        url,
        data=body,
        timeout=30,
    )

    resp.raise_for_status()

    resp.encoding = (
        resp.apparent_encoding
        or "utf-8"
    )

    html = resp.text

    print(
        f"[LIST] "
        f"menu={menu_key} "
        f"status={resp.status_code} "
        f"url={resp.url} "
        f"length={len(html)}"
    )

    if looks_like_login_page(
        html,
        resp.url,
    ):

        raise SessionExpiredError(
            f"Сессия истекла при запросе "
            f"списка для {menu_key}."
        )

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    rows = (
        soup.select(
            "table.boardList tr"
        )
        or
        soup.select(
            "table tr"
        )
    )

    posts = []

    for row in rows:

        link = (
            row.select_one(
                "a.selectListTitle"
            )
            or
            row.select_one(
                'a[href*="viewBbs.do"]'
            )
        )

        if not link:
            continue

        href = (
            link.get("href", "")
            or
            link.get("onclick", "")
        )

        m = re.search(
            r"NOTIWR_NO=(\d+)",
            href,
        )

        if not m:

            m = re.search(
                r"NOTIWR_NO['\"]?"
                r"\s*[,=]\s*"
                r"['\"]?(\d+)",
                str(row),
            )

        if not m:
            continue

        post_id = m.group(1)

        cells = row.find_all("td")

        author = (
            cells[-3].get_text(
                strip=True
            )
            if len(cells) >= 3
            else ""
        )

        date = (
            cells[-2].get_text(
                strip=True
            )
            if len(cells) >= 2
            else ""
        )

        views = (
            cells[-1].get_text(
                strip=True
            )
            if len(cells) >= 1
            else ""
        )

        title = link.get_text(
            " ",
            strip=True,
        )

        if not title:
            continue

        posts.append(
            {
                "post_id": post_id,
                "title": title,
                "author": author,
                "date": date,
                "views": views,
            }
        )

    return posts[:POSTS_PER_CATEGORY]


# ============================================================
# FETCH DETAIL
# ============================================================

def fetch_detail(
    session: requests.Session,
    menu_key: str,
    board_id: str,
    post_id: str,
):

    url = (
        f"{BASE_URL}"
        f"/s/potal_board/"
        f"{menu_key}/viewBbs.do"
    )

    params = {
        "NOTIWR_NO": post_id,
        "currentPage": 1,
        "BOARD_ID": board_id,
        "SITE_KEY": SITE_KEY,
        "MENU_KEY": menu_key,
        "language": LANGUAGE,
    }

    resp = session.get(
        url,
        params=params,
        timeout=30,
    )

    resp.raise_for_status()

    resp.encoding = (
        resp.apparent_encoding
        or "utf-8"
    )

    html = resp.text

    print(
        f"[DETAIL] "
        f"post={post_id} "
        f"status={resp.status_code} "
        f"url={resp.url} "
        f"length={len(html)}"
    )

    if looks_like_login_page(
        html,
        resp.url,
    ):

        raise SessionExpiredError(
            f"Сессия истекла при открытии "
            f"поста {post_id}."
        )

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    eng = soup.select_one(
        "tr#cntn_eng_view"
    )

    kor = soup.select_one(
        "tr#cntn_kor_view"
    )

    fallback = soup.select_one(
        "td.boardViewBody"
    )

    text = ""

    if eng and eng.get_text(
        strip=True
    ):

        text = eng.get_text(
            "\n",
            strip=True,
        )

    elif kor and kor.get_text(
        strip=True
    ):

        text = kor.get_text(
            "\n",
            strip=True,
        )

    elif fallback:

        text = fallback.get_text(
            "\n",
            strip=True,
        )

    # Дополнительный fallback.
    # Иногда структура страницы меняется.
    if not text:

        candidates = soup.select(
            "td, div"
        )

        largest = ""

        for element in candidates:

            value = element.get_text(
                " ",
                strip=True,
            )

            if len(value) > len(largest):
                largest = value

        text = largest

    attachments = []

    for block in soup.select(
        "div.bbs_single_file, "
        "div.bbs_file"
    ):

        for a in block.find_all(
            attrs={"onclick": True}
        ):

            onclick = a.get(
                "onclick",
                "",
            )

            m = re.search(
                r"fnDownloadFile\("
                r"\s*['\"]?([^'\",)]+)"
                r"['\"]?"
                r"\s*,\s*"
                r"['\"]?([^'\",)]+)"
                r"['\"]?\s*\)",
                onclick,
            )

            if m:

                attachments.append(
                    {
                        "conn_no": m.group(1),
                        "seq_no": m.group(2),
                        "label": a.get_text(
                            strip=True
                        ),
                    }
                )

    return {
        "text": text,
        "attachments": attachments,
    }


# ============================================================
# GEMINI
# ============================================================

def summarize_with_gemini(
    title: str,
    text: str,
    category: str,
) -> dict:

    """
    Возвращает:

    {
        "important": bool,
        "summary": str
    }

    ВАЖНО:
    Ошибка Gemini НИКОГДА не должна валить весь мониторинг.

    Особенно 429 quota exceeded.
    В таком случае используем обычный preview.
    """

    fallback = make_preview(
        text,
        max_len=300,
    )

    if not fallback:

        fallback = (
            "Описание объявления "
            "недоступно."
        )

    if not GEMINI_API_KEY:

        return {
            "important": True,
            "summary": fallback,
        }

    prompt = (
        f"Ты помогаешь студенту DGIST "
        f"понимать объявления университета.\n\n"

        f"Раздел: {category}\n"
        f"Заголовок: {title}\n\n"

        f"Текст объявления:\n"
        f"{text[:5000]}\n\n"

        "Сделай очень краткое описание "
        "на русском языке, максимум 2 предложения.\n\n"

        "Также определи, является ли объявление "
        "важным для обычного студента.\n\n"

        "important=true если это, например:\n"
        "- дедлайн;\n"
        "- стипендия;\n"
        "- обязательное действие;\n"
        "- изменение расписания;\n"
        "- важное изменение правил;\n"
        "- важное объявление университета.\n\n"

        "important=false для обычных новостей, "
        "мероприятий, меню, несущественных объявлений.\n\n"

        "Ответь СТРОГО JSON:\n"
        '{"important": true, '
        '"summary": "краткое описание"}'
    )

    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
        f"?key={GEMINI_API_KEY}"
    )

    try:

        resp = requests.post(
            url,
            headers={
                "content-type":
                    "application/json"
            },
            json={
                "contents": [
                    {
                        "parts": [
                            {
                                "text": prompt
                            }
                        ]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": 200,
                    "responseMimeType":
                        "application/json",
                },
            },
            timeout=30,
        )

    except Exception as e:

        print(
            f"[GEMINI] Request error: {e}"
        )

        return {
            "important": True,
            "summary": fallback,
        }

    # --------------------------------------------------------
    # 429 — НЕ ПАДАЕМ
    # --------------------------------------------------------

    if resp.status_code == 429:

        print(
            "[GEMINI] 429 quota exceeded. "
            "Использую fallback preview."
        )

        return {
            "important": True,
            "summary": fallback,
        }

    # --------------------------------------------------------
    # Другие ошибки
    # --------------------------------------------------------

    if not resp.ok:

        try:
            error_data = resp.json()
        except Exception:
            error_data = resp.text[:500]

        print(
            f"[GEMINI] API error "
            f"{resp.status_code}: "
            f"{error_data}"
        )

        return {
            "important": True,
            "summary": fallback,
        }

    try:

        data = resp.json()

        candidates = data.get(
            "candidates",
            [],
        )

        parts = candidates[0][
            "content"
        ][
            "parts"
        ]

        raw_text = "".join(
            p.get("text", "")
            for p in parts
        )

    except (
        IndexError,
        KeyError,
        TypeError,
    ):

        print(
            "[GEMINI] Не удалось "
            "разобрать ответ."
        )

        return {
            "important": True,
            "summary": fallback,
        }

    raw_text = raw_text.strip()

    # Убираем markdown JSON
    raw_text = (
        raw_text
        .replace("```json", "")
        .replace("```", "")
        .strip()
    )

    if raw_text.lower().startswith(
        "json"
    ):

        raw_text = raw_text[
            4:
        ].strip()

    try:

        parsed = json.loads(
            raw_text
        )

        summary = str(
            parsed.get(
                "summary",
                "",
            )
        ).strip()

        important = bool(
            parsed.get(
                "important",
                True,
            )
        )

        if not summary:
            summary = fallback

        return {
            "important": important,
            "summary": summary[:700],
        }

    except Exception:

        print(
            "[GEMINI] Ответ не является "
            "валидным JSON."
        )

        return {
            "important": True,
            "summary": (
                raw_text[:700]
                if raw_text
                else fallback
            ),
        }


# ============================================================
# CREATE ENTRY
# ============================================================

def make_entry(
    post: dict,
    category_name: str,
    verdict: dict,
    attachments: list,
) -> dict:

    return {
        "timestamp":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "category":
            category_name,

        "post_id":
            post["post_id"],

        "title":
            post["title"],

        "date":
            post["date"],

        "author":
            post["author"],

        "important":
            bool(
                verdict.get(
                    "important",
                    True,
                )
            ),

        "summary":
            verdict.get(
                "summary",
                "",
            ),

        "attachments":
            attachments or [],
    }


# ============================================================
# MAIN
# ============================================================

def run_once(
    cookie: str = None,
    state_file: str = None,
    log_file: str = None,
):

    """
    Проверяет DGIST ОДИН РАЗ.

    Главное отличие от старой версии:

    - НЕ строит digest топ-10;
    - НЕ обрабатывает старые объявления;
    - при первом запуске только создаёт baseline;
    - дальше возвращает только новые посты.
    """

    session = build_session(
        cookie
    )

    seen = load_seen(
        state_file
    )

    new_important = []
    new_minor = []

    # --------------------------------------------------------
    # FIRST RUN
    # --------------------------------------------------------

    is_first_run = not bool(seen)

    if is_first_run:

        print(
            "[MONITOR] Первый запуск. "
            "Создаю baseline из текущих "
            "объявлений. Старые посты "
            "отправляться не будут."
        )

    # Заголовки, уже обработанные в этом
    # конкретном запуске.
    processed_titles = set()

    for menu_key, (
        board_id,
        category_name,
    ) in CATEGORIES.items():

        try:

            posts = fetch_list(
                session,
                menu_key,
                board_id,
            )

        except SessionExpiredError as e:

            print(
                f"[СЕССИЯ ИСТЕКЛА] {e}"
            )

            return {
                "new_important": [],
                "new_minor": [],
                "digest_top10": [],
                "error": str(e),
            }

        except Exception as e:

            print(
                f"[LIST ERROR] "
                f"{menu_key}: {e}"
            )

            continue

        time.sleep(
            REQUEST_DELAY_SEC
        )

        existing_ids = set(
            seen.get(
                menu_key,
                [],
            )
        )

        current_ids = {
            p["post_id"]
            for p in posts
        }

        # ----------------------------------------------------
        # FIRST RUN:
        # просто запоминаем всё.
        # ----------------------------------------------------

        if is_first_run:

            existing_ids.update(
                current_ids
            )

            seen[menu_key] = list(
                existing_ids
            )

            print(
                f"[BASELINE] "
                f"{category_name}: "
                f"{len(current_ids)} "
                f"постов запомнено."
            )

            continue

        # ----------------------------------------------------
        # NORMAL RUN:
        # только новые post_id
        # ----------------------------------------------------

        fresh = [
            p
            for p in posts
            if p["post_id"]
            not in existing_ids
        ]

        if fresh:

            print(
                f"[NEW] "
                f"{category_name}: "
                f"{len(fresh)} новых."
            )

        for post in fresh:

            post_id = post[
                "post_id"
            ]

            title_key = (
                post["title"]
                .strip()
                .lower()
            )

            # ------------------------------------------------
            # CROSS-POST DEDUP
            # ------------------------------------------------

            if title_key in processed_titles:

                print(
                    f"[DEDUP] "
                    f"{post['title']}"
                )

                existing_ids.add(
                    post_id
                )

                continue

            try:

                detail = fetch_detail(
                    session,
                    menu_key,
                    board_id,
                    post_id,
                )

            except SessionExpiredError as e:

                print(
                    f"[СЕССИЯ ИСТЕКЛА] {e}"
                )

                seen[menu_key] = list(
                    existing_ids
                )
                save_seen(
                    seen,
                    state_file,
                )

                return {
                    "new_important":
                        new_important,

                    "new_minor":
                        new_minor,

                    "digest_top10":
                        [],

                    "error":
                        str(e),
                }

            except Exception as e:

                print(
                    f"[DETAIL ERROR] "
                    f"post={post_id}: "
                    f"{e}"
                )

                # Всё равно помечаем его увиденным,
                # чтобы один сломанный пост не
                # обрабатывался бесконечно.
                existing_ids.add(
                    post_id
                )

                continue

            time.sleep(
                REQUEST_DELAY_SEC
            )

            # ------------------------------------------------
            # GEMINI
            # ------------------------------------------------

            verdict = (
                summarize_with_gemini(
                    post["title"],
                    detail["text"],
                    category_name,
                )
            )

            processed_titles.add(
                title_key
            )

            entry = make_entry(
                post,
                category_name,
                verdict,
                detail["attachments"],
            )

            append_log(
                entry,
                log_file,
            )

            if entry["important"]:

                new_important.append(
                    entry
                )

            else:

                new_minor.append(
                    entry
                )

            existing_ids.add(
                post_id
            )

            print(
                f"[PROCESSED] "
                f"{category_name} | "
                f"{post['title']}"
            )

        # ----------------------------------------------------
        # SAVE SEEN IDS
        # ----------------------------------------------------

        seen[menu_key] = list(
            existing_ids
        )

    # --------------------------------------------------------
    # SAVE STATE
    # --------------------------------------------------------

    save_seen(
        seen,
        state_file,
    )

    print(
        f"[RESULT] "
        f"new_important="
        f"{len(new_important)}, "
        f"new_minor="
        f"{len(new_minor)}"
    )

    # --------------------------------------------------------
    # IMPORTANT:
    # digest_top10 оставляем для совместимости
    # со старым dgist_bot_handlers.py.
    #
    # Но теперь он ВСЕГДА пустой.
    # --------------------------------------------------------

    return {
        "new_important":
            new_important,

        "new_minor":
            new_minor,

        "digest_top10":
            [],

        "error":
            None,
    }


# ============================================================
# TELEGRAM ENTRY POINT
# ============================================================

def run_once_for_user(
    telegram_user_id: int,
    dgist_cookie=None,
) -> dict:

    """
    Основная функция для Telegram-бота.

    Поддерживает два варианта:

    1. dgist_cookie = строка Cookie

    2. dgist_cookie = dict account
       (на случай если текущий bot handler
       передаёт сюда весь account).

    Если cookie не передана — пытаемся прочитать её
    из user_data/<telegram_id>/session_cookie.txt.
    """

    paths = build_user_paths(
        telegram_user_id
    )

    cookie = None

    # --------------------------------------------------------
    # Если передали обычную строку
    # --------------------------------------------------------

    if isinstance(
        dgist_cookie,
        str,
    ):

        cookie = dgist_cookie.strip()

    # --------------------------------------------------------
    # Если текущий bot handler передаёт account dict
    # --------------------------------------------------------

    elif isinstance(
        dgist_cookie,
        dict,
    ):

        possible_keys = (
            "dgist_cookie",
            "cookie",
            "session_cookie",
        )

        for key in possible_keys:

            value = dgist_cookie.get(
                key
            )

            if value:

                cookie = str(
                    value
                ).strip()

                break

    # --------------------------------------------------------
    # Если cookie не передали —
    # читаем user_data/.../session_cookie.txt
    # --------------------------------------------------------

    if not cookie:

        cookie_file = paths[
            "cookie_file"
        ]

        if os.path.exists(
            cookie_file
        ):

            with open(
                cookie_file,
                "r",
                encoding="utf-8",
            ) as f:

                cookie = f.read().strip()

    # --------------------------------------------------------
    # Fallback на старую глобальную cookie
    # --------------------------------------------------------

    if not cookie:

        cookie = SESSION_COOKIE

    if not cookie:

        raise RuntimeError(
            f"Не найдена DGIST cookie "
            f"для Telegram user "
            f"{telegram_user_id}."
        )

    return run_once(
        cookie=cookie,

        state_file=paths[
            "state_file"
        ],

        log_file=paths[
            "log_file"
        ],
    )


# ============================================================
# DIRECT RUN
# ============================================================

if __name__ == "__main__":

    result = run_once()

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )