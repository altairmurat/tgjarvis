#!/usr/bin/env python3
"""
DGIST Bulletin Board Monitor
-----------------------------
Периодически проверяет несколько разделов доски объявлений DGIST,
находит новые посты (по сравнению с прошлым запуском), скачивает
их содержимое и прогоняет через Gemini API для оценки важности
и краткого summary.

ВАЖНО:
- Требует действующую cookie сессии (JSESSIONID/SSO), полученную
  после ручного логина + подтверждения кодом с почты в браузере.
- Скрипт НЕ логинится сам и не обходит 2FA — он просто переиспользует
  уже установленную сессию, как это делает сам браузер при обычной
  навигации по сайту.
- Когда сессия истечёт, запросы начнут возвращать страницу логина
  вместо данных — скрипт это определяет и говорит, что пора обновить cookie.
"""

import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()  # подхватывает переменные из .env, если он лежит рядом со скриптом

# ========================= КОНФИГУРАЦИЯ =========================

BASE_URL = "https://stuecm.dgist.ac.kr"

# Разделы, которые мониторим: menu_key -> (board_id, человекочитаемое имя)
CATEGORIES = {
    "_0031": ("PB_ST00220031", "Latest / Student Bulletin"),
    "_0014": ("PB_ST00220014", "Academic Notice"),
    "_0161": ("BD000240",      "Scholarships"),
    "_0162": ("PB_ST00220162", "Career & Jobs"),
    "_0004": ("PB_ST00220004", "Student Support"),
}

SITE_KEY = "ST0022"
LANGUAGE = "en"

# Сколько последних постов на категорию проверять за раз
POSTS_PER_CATEGORY = 10

# Файлы состояния (что уже видели) и вывода
STATE_FILE = os.path.join(os.path.dirname(__file__), "seen_ids.json")
LOG_FILE = os.path.join(os.path.dirname(__file__), "summaries_log.jsonl")

# Cookie сессии — сначала пробуем прочитать файл, который обновляет
# refresh_session.py, если файла нет/пуст — берём из переменной окружения
SESSION_COOKIE_FILE = os.path.join(os.path.dirname(__file__), "session_cookie.txt")

# =====================================================================
# ДЛЯ TELEGRAM-БОТА (МУЛЬТИ-ЮЗЕР): всё, что выше — однопользовательский
# режим для .env. Как только подключаешь бота с несколькими людьми,
# STATE_FILE / SESSION_COOKIE_FILE / профиль браузера должны стать
# per-user, иначе данные разных людей будут перезаписывать друг друга.
#
# Смотри build_user_paths() ниже и функцию run_once_for_user() —
# это те самые места, которые нужно трогать для интеграции с ботом.
# =====================================================================


def build_user_paths(telegram_user_id: int) -> dict:
    """
    Возвращает пути к файлам состояния КОНКРЕТНОГО юзера, изолированные
    по его Telegram ID. telegram_user_id должен браться ТОЛЬКО из
    update.effective_user.id на стороне бота — никогда из текста
    сообщения, callback_data кнопки или чего-либо ещё, что теоретически
    можно подделать.
    """
    base = os.path.join(os.path.dirname(__file__), "user_data", str(telegram_user_id))
    os.makedirs(base, exist_ok=True)
    return {
        "state_file": os.path.join(base, "seen_ids.json"),
        "log_file": os.path.join(base, "summaries_log.jsonl"),
        "cookie_file": os.path.join(base, "session_cookie.txt"),
        "browser_profile_dir": os.path.join(base, "browser_profile"),
    }


def _load_session_cookie() -> str:
    if os.path.exists(SESSION_COOKIE_FILE):
        with open(SESSION_COOKIE_FILE, "r", encoding="utf-8") as f:
            value = f.read().strip()
            if value:
                return value
    return os.environ.get("DGIST_COOKIE", "")


SESSION_COOKIE = _load_session_cookie()

# Ключ Gemini API — тоже через переменную окружения
# Получить можно в Google AI Studio: https://aistudio.google.com/apikey
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": BASE_URL,
}

REQUEST_DELAY_SEC = 1.0  # пауза между запросами, чтобы не долбить сервер

# ========================= ВСПОМОГАТЕЛЬНОЕ =========================


def build_session(cookie: str = None) -> requests.Session:
    cookie = cookie if cookie is not None else SESSION_COOKIE
    if not cookie:
        raise RuntimeError(
            "Не найдена cookie сессии ни в session_cookie.txt, ни в DGIST_COOKIE. "
            "Сначала выполни авторизацию через auto_login.py."
        )
    s = requests.Session()
    s.headers.update(HEADERS)
    # auto_login.py после ECM SSO сохраняет cookie всех DGIST-доменов.
    # Передаём их как единый Cookie header, чтобы requests использовал
    # состояние, которое реально создал Chromium.
    s.headers["Cookie"] = cookie
    return s


def looks_like_login_page(html: str, response_url: str = "") -> bool:
    """
    Проверяет, действительно ли ответ является страницей login/SSO.

    ВАЖНО: нельзя считать страницу логином только потому, что в HTML
    встречается слово ``password`` — оно вполне может присутствовать
    в JavaScript или обычном содержимом страницы объявления.
    """
    html = html or ""
    lowered = html.lower()
    url = (response_url or "").lower()

    # Если requests реально был перенаправлен на login/SSO — это сильный
    # и надёжный признак истёкшей сессии.
    login_url_markers = (
        "isign.dgist.ac.kr/login.html",
        "/login.html",
        "/login.do",
        "/sso/login",
    )
    if any(marker in url for marker in login_url_markers):
        return True

    # Более специфичные признаки HTML login-формы. Одного общего слова
    # password недостаточно.
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
    strong_count = sum(marker in lowered for marker in strong_markers)
    if strong_count >= 2:
        return True

    # Именно тот SSO-ответ, который ты уже видел у DGIST.
    if (
        "sendurl" in lowered
        and "isign.dgist.ac.kr/login.html" in lowered
        and "agentid" in lowered
    ):
        return True

    return False


def load_seen(state_file: str = None) -> dict:
    state_file = state_file or STATE_FILE
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(seen: dict, state_file: str = None) -> None:
    state_file = state_file or STATE_FILE
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def append_log(entry: dict, log_file: str = None) -> None:
    log_file = log_file or LOG_FILE
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def make_preview(text: str, max_len: int = 160) -> str:
    """Короткий превью-сниппет сырого текста поста (без обращения к Gemini) —
    используется для постов в дайджесте, которые уже видели раньше и поэтому
    не гоняли через Gemini заново."""
    text = " ".join((text or "").split())  # схлопнуть переносы строк/пробелы
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "…"


# ========================= СПИСОК ПОСТОВ =========================


def fetch_list(session: requests.Session, menu_key: str, board_id: str, page: int = 1):
    url = f"{BASE_URL}/s/potal_board/{menu_key}/listBbs.do"
    body = {
        "currentPage": page,
        "BOARD_ID": board_id,
        "SITE_KEY": SITE_KEY,
        "MENU_KEY": menu_key,
        "language": LANGUAGE,
    }
    resp = session.post(url, data=body, timeout=15)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    html = resp.text

    print(
        f"[LIST] menu={menu_key} status={resp.status_code} "
        f"url={resp.url} length={len(html)}"
    )

    if looks_like_login_page(html, resp.url):
        raise SessionExpiredError(
            f"Сессия истекла при запросе списка для {menu_key}. "
            "Зайди в браузер, залогинься заново, обнови DGIST_COOKIE."
        )

    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table.boardList tr") or soup.select("table tr")

    posts = []
    for row in rows:
        link = row.select_one("a.selectListTitle") or row.select_one('a[href*="viewBbs.do"]')
        if not link:
            continue  # это, скорее всего, строка заголовка таблицы

        href = link.get("href", "") or link.get("onclick", "")
        m = re.search(r"NOTIWR_NO=(\d+)", href)
        if not m:
            # иногда ID передаётся через onclick-JS, а не href — пробуем шире
            m = re.search(r"NOTIWR_NO['\"]?\s*[,=]\s*['\"]?(\d+)", str(row))
        if not m:
            continue
        post_id = m.group(1)

        cells = row.find_all("td")
        author = cells[-3].get_text(strip=True) if len(cells) >= 3 else ""
        date = cells[-2].get_text(strip=True) if len(cells) >= 2 else ""
        views = cells[-1].get_text(strip=True) if len(cells) >= 1 else ""

        posts.append({
            "post_id": post_id,
            "title": link.get_text(strip=True),
            "author": author,
            "date": date,
            "views": views,
        })

    return posts[:POSTS_PER_CATEGORY]


class SessionExpiredError(Exception):
    pass


# ========================= ДЕТАЛИ ПОСТА =========================


def fetch_detail(session: requests.Session, menu_key: str, board_id: str, post_id: str):
    url = f"{BASE_URL}/s/potal_board/{menu_key}/viewBbs.do"
    params = {
        "NOTIWR_NO": post_id,
        "currentPage": 1,
        "BOARD_ID": board_id,
        "SITE_KEY": SITE_KEY,
        "MENU_KEY": menu_key,
        "language": LANGUAGE,
    }
    resp = session.get(url, params=params, timeout=15)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    html = resp.text

    print(
        f"[DETAIL] post={post_id} status={resp.status_code} "
        f"url={resp.url} length={len(html)}"
    )

    if looks_like_login_page(html, resp.url):
        raise SessionExpiredError(
            f"Сессия истекла при открытии поста {post_id}. "
            f"Final URL: {resp.url}. Обнови DGIST_COOKIE."
        )

    soup = BeautifulSoup(html, "html.parser")

    eng = soup.select_one("tr#cntn_eng_view")
    kor = soup.select_one("tr#cntn_kor_view")
    fallback = soup.select_one("td.boardViewBody")

    text = ""
    if eng and eng.get_text(strip=True):
        text = eng.get_text("\n", strip=True)
    elif kor and kor.get_text(strip=True):
        text = kor.get_text("\n", strip=True)
    elif fallback:
        text = fallback.get_text("\n", strip=True)

    attachments = []
    for block in soup.select("div.bbs_single_file, div.bbs_file"):
        for a in block.find_all(attrs={"onclick": True}):
            m = re.search(r"fnDownloadFile\(\s*['\"]?([^'\",)]+)['\"]?\s*,\s*['\"]?([^'\",)]+)['\"]?\s*\)", a["onclick"])
            if m:
                attachments.append({
                    "conn_no": m.group(1),
                    "seq_no": m.group(2),
                    "label": a.get_text(strip=True),
                })

    return {"text": text, "attachments": attachments}


def download_attachment(session: requests.Session, conn_no: str, seq_no: str, out_dir: str):
    url = f"{BASE_URL}/ecm/ecmCommon/file/downloadFile.do"
    params = {"ATFILE_CONN_NO": conn_no, "ATFILE_SEQ_NO": seq_no}
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()

    filename = f"{conn_no}_{seq_no}"
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.IGNORECASE)
    if m:
        from urllib.parse import unquote
        filename = unquote(m.group(1))
    else:
        m = re.search(r'filename="?([^";]+)"?', cd)
        if m:
            filename = m.group(1)

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    with open(path, "wb") as f:
        f.write(resp.content)
    return path


# ========================= СУММАРИЗАЦИЯ ЧЕРЕЗ GEMINI =========================


def summarize_with_gemini(title: str, text: str, category: str) -> dict:
    """
    Возвращает {"important": bool, "summary": str}.
    Если ключ API не задан — просто пропускает важность/summary (важно=True по умолчанию),
    чтобы можно было проверить сбор данных без затрат на API.
    """
    if not GEMINI_API_KEY:
        return {"important": True, "summary": "(GEMINI_API_KEY не задан — суммаризация пропущена)"}

    prompt = (
        f"Раздел доски объявлений DGIST: {category}\n"
        f"Заголовок: {title}\n\n"
        f"Текст объявления:\n{text[:4000]}\n\n"
        "Ответь СТРОГО в формате JSON без пояснений вокруг и без markdown-обёртки:\n"
        '{"important": true/false, "summary": "краткое summary на русском, 1-3 предложения"}\n'
        "important=true если это стипендии, дедлайны, обязательные для студентов вещи, "
        "изменения в расписании/правилах, важные объявления деканата. "
        "important=false если это рутинные новости, меню столовой, мелкие внутренние события."
    )

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    resp = requests.post(
        url,
        headers={"content-type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 300,
                # Просим модель вернуть чистый JSON без ```-обёртки
                "responseMimeType": "application/json",
            },
        },
        timeout=30,
    )
    if not resp.ok:
        # Google currently restricts access to Gemini 2.5 models for users
        # who have actively used them before. If an old config still points
        # to 2.5, give a useful message instead of crashing the whole portal check.
        try:
            err = resp.json()
        except ValueError:
            err = resp.text[:500]
        raise RuntimeError(
            f"Gemini API error {resp.status_code} for model {GEMINI_MODEL}: {err}. "
            "Set GEMINI_MODEL=gemini-3.5-flash-lite (or another currently available model)."
        )
    data = resp.json()

    try:
        candidates = data.get("candidates", [])
        parts = candidates[0]["content"]["parts"]
        raw_text = "".join(p.get("text", "") for p in parts)
    except (IndexError, KeyError):
        return {"important": True, "summary": f"(не удалось разобрать ответ Gemini: {data})"}

    raw_text = raw_text.strip().strip("`")
    if raw_text.lower().startswith("json"):
        raw_text = raw_text[4:].strip()

    try:
        parsed = json.loads(raw_text)
        return {"important": bool(parsed.get("important", True)), "summary": parsed.get("summary", "")}
    except json.JSONDecodeError:
        return {"important": True, "summary": raw_text[:300]}


# ========================= ОСНОВНОЙ ЦИКЛ =========================


def run_once(cookie: str = None, state_file: str = None, log_file: str = None):
    """
    cookie / state_file / log_file — необязательные оверрайды. Если не переданы,
    берутся глобальные однопользовательские значения (для .env-режима).
    Для бота используй run_once_for_user() ниже вместо прямого вызова этой функции.
    """
    session = build_session(cookie)
    seen = load_seen(state_file)
    new_important = []
    new_minor = []
    all_posts_this_run = []  # для дайджеста "топ-10 по всем разделам" в конце

    # DGIST часто кросс-постит один и тот же пост сразу в "Latest" и в
    # тематический раздел (Scholarships/Career/...) — это два РАЗНЫХ post_id
    # с ОДИНАКОВЫМ заголовком. Чтобы не суммаризировать через Gemini дважды
    # и не слать одно и то же объявление два раза, отслеживаем заголовки,
    # которые уже обработали в рамках этого запуска, и переиспользуем
    # готовый summary для дайджеста вместо повторного похода в Gemini.
    already_summarized_titles = set()
    post_id_to_summary = {}

    for menu_key, (board_id, category_name) in CATEGORIES.items():
        seen_ids = set(seen.get(menu_key, []))
        try:
            posts = fetch_list(session, menu_key, board_id)
        except SessionExpiredError as e:
            print(f"[СЕССИЯ ИСТЕКЛА] {e}")
            return {
                "new_important": [],
                "new_minor": [],
                "digest_top10": [],
                "error": str(e),
            }
        time.sleep(REQUEST_DELAY_SEC)

        # Помечаем категорией и складываем ВСЕ посты (не только новые) —
        # они понадобятся для общего дайджеста топ-10 в конце. Сохраняем
        # тут же menu_key/board_id, чтобы потом можно было дозапросить
        # детали конкретно ЭТОГО поста для превью в дайджесте.
        for p in posts:
            all_posts_this_run.append({
                **p,
                "category": category_name,
                "menu_key": menu_key,
                "board_id": board_id,
            })

        fresh = [p for p in posts if p["post_id"] not in seen_ids]

        for post in fresh:
            title_key = post["title"].strip().lower()

            if title_key in already_summarized_titles:
                # Уже видели этот же заголовок в другой категории в ЭТОМ
                # запуске — помечаем как просмотренный (чтобы не всплыл
                # снова в следующий раз), но не тратим вызов Gemini и не
                # шлём повторное уведомление.
                seen_ids.add(post["post_id"])
                continue

            try:
                detail = fetch_detail(session, menu_key, board_id, post["post_id"])
            except SessionExpiredError as e:
                print(f"[СЕССИЯ ИСТЕКЛА] {e}")
                return {
                    "new_important": [],
                    "new_minor": [],
                    "digest_top10": [],
                    "error": str(e),
                }
            time.sleep(REQUEST_DELAY_SEC)

            verdict = summarize_with_gemini(post["title"], detail["text"], category_name)
            already_summarized_titles.add(title_key)
            post_id_to_summary[post["post_id"]] = verdict["summary"]

            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "category": category_name,
                "post_id": post["post_id"],
                "title": post["title"],
                "date": post["date"],
                "author": post["author"],
                "important": verdict["important"],
                "summary": verdict["summary"],
                "attachments": detail["attachments"],
            }
            append_log(entry, log_file)

            if verdict["important"]:
                new_important.append(entry)
            else:
                new_minor.append(entry)

            seen_ids.add(post["post_id"])

        seen[menu_key] = list(seen_ids)

    save_seen(seen, state_file)

    print(f"Новых важных: {len(new_important)}, новых прочих: {len(new_minor)}")
    for e in new_important:
        print(f"\n=== [{e['category']}] {e['title']} ({e['date']}) ===")
        print(e["summary"])

    # Дайджест считается ВСЕГДА, даже если новых постов не было вообще —
    # его данные (не только print) отдаём наружу, чтобы бот мог переслать
    # их в Телеграм без парсинга консольного вывода
    def digest_sort_key(p):
        try:
            post_id_num = int(p["post_id"])
        except (ValueError, TypeError):
            post_id_num = 0
        return (p.get("date", ""), post_id_num)

    # Дедуп по заголовку и для дайджеста тоже — иначе кросс-посты из
    # "Latest" будут дублировать записи из тематических разделов
    seen_titles_digest = set()
    deduped_posts = []
    for p in sorted(all_posts_this_run, key=digest_sort_key, reverse=True):
        title_key = p["title"].strip().lower()
        if title_key in seen_titles_digest:
            continue
        seen_titles_digest.add(title_key)
        deduped_posts.append(p)

    digest_top10 = deduped_posts[:10]

    # Добавляем короткое описание к каждому посту дайджеста:
    # - если пост обрабатывался в этом запуске как новый — переиспользуем
    #   уже готовый Gemini-summary (бесплатно, он уже посчитан выше);
    # - если пост старый (уже видели раньше) — гоняем через Gemini его
    #   не будем (незачем платить за то, что не изменилось), а просто
    #   подтягиваем сырой текст поста и берём короткий сниппет из него.
    for p in digest_top10:
        pid = p["post_id"]
        if pid in post_id_to_summary:
            p["description"] = post_id_to_summary[pid]
            continue

        try:
            detail = fetch_detail(session, p["menu_key"], p["board_id"], pid)
            p["description"] = make_preview(detail["text"])
            time.sleep(REQUEST_DELAY_SEC)
        except SessionExpiredError:
            p["description"] = ""
        except Exception as e:
            print(f"[digest preview error] post={pid}: {e}")
            p["description"] = ""

    print(f"\n=== Топ-10 последних постов по всем разделам ===")
    for p in digest_top10:
        print(f"[{p['category']}] {p['date']} — {p['title']}")
        if p.get("description"):
            print(f"    {p['description']}")

    return {
        "new_important": new_important,
        "new_minor": new_minor,
        "digest_top10": digest_top10,
        "error": None,
    }


def run_once_for_user(telegram_user_id: int, dgist_cookie: str) -> dict:
    """
    ТОЧКА ВХОДА ДЛЯ ТЕЛЕГРАМ-БОТА.

    telegram_user_id — бери ИСКЛЮЧИТЕЛЬНО из update.effective_user.id
    на стороне бота. Никогда не передавай сюда значение, взятое из
    текста сообщения или callback_data кнопки — это единственное,
    что защищает от "чужих" данных.

    dgist_cookie — актуальная cookie сессии ЭТОГО юзера. Как её получить —
    отдельный вопрос (см. пояснение про auto_login.py ниже в чате), но
    сюда должна прийти уже готовая строка cookie именно для этого
    telegram_user_id, а не какая-то общая/чужая.

    Все файлы состояния (seen_ids.json, лог) автоматически изолируются
    в user_data/<telegram_user_id>/ — так что даже если один и тот же
    процесс бота обслуживает 500 разных студентов, они не смешаются.
    """
    paths = build_user_paths(telegram_user_id)
    return run_once(
        cookie=dgist_cookie,
        state_file=paths["state_file"],
        log_file=paths["log_file"],
    )


if __name__ == "__main__":
    run_once()