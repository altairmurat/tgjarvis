#!/usr/bin/env python3
"""
debug_listbbs.py
------------------
Диагностика: почему fetch_list() в dgist_monitor.py находит 0 постов.

Запускает тот же самый запрос listBbs.do, что и основной скрипт,
сохраняет сырой HTML в debug_listbbs.html и печатает статистику по
текущим селекторам, чтобы понять, что именно не совпадает.

ЗАПУСК:
    python debug_listbbs.py <telegram_user_id>

Если запускаешь не через бота, а как одиночный .env-режим (dgist_monitor.py
с session_cookie.txt рядом) — просто:
    python debug_listbbs.py --single
"""

import sys
import os
import requests
from bs4 import BeautifulSoup

import dgist_monitor as dm

MENU_KEY = "_0031"          # категория "Latest / Student Bulletin"
BOARD_ID = "PB_ST00220031"


def get_cookie(arg: str) -> str:
    if arg == "--single":
        path = os.path.join(os.path.dirname(__file__), "session_cookie.txt")
    else:
        telegram_user_id = int(arg)
        paths = dm.build_user_paths(telegram_user_id)
        path = paths["cookie_file"]

    with open(path, "r", encoding="utf-8") as f:
        cookie = f.read().strip()
    print(f"Cookie прочитана из: {path} (длина {len(cookie)} символов)\n")
    return cookie


def main():
    if len(sys.argv) < 2:
        print("Использование: python debug_listbbs.py <telegram_user_id>  ИЛИ  python debug_listbbs.py --single")
        sys.exit(1)

    cookie = get_cookie(sys.argv[1])

    session = requests.Session()
    session.headers.update(dm.HEADERS)
    session.headers["Cookie"] = cookie

    url = f"{dm.BASE_URL}/s/potal_board/{MENU_KEY}/listBbs.do"
    body = {
        "currentPage": 1,
        "BOARD_ID": BOARD_ID,
        "SITE_KEY": dm.SITE_KEY,
        "MENU_KEY": MENU_KEY,
        "language": dm.LANGUAGE,
    }

    resp = session.post(url, data=body, timeout=15)
    resp.encoding = resp.apparent_encoding or "utf-8"
    html = resp.text

    out_path = os.path.join(os.path.dirname(__file__), "debug_listbbs.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTTP статус: {resp.status_code}")
    print(f"Полный HTML сохранён в: {out_path}")
    print(f"Длина ответа: {len(html)} символов\n")

    print("--- Похоже ли на страницу логина? ---")
    print(dm.looks_like_login_page(html))
    print()

    soup = BeautifulSoup(html, "html.parser")

    print("--- Диагностика текущих селекторов ---")
    rows_boardlist = soup.select("table.boardList tr")
    print(f"table.boardList tr -> найдено строк: {len(rows_boardlist)}")

    rows_any_table = soup.select("table tr")
    print(f"table tr (любая таблица) -> найдено строк: {len(rows_any_table)}")

    links_selectlisttitle = soup.select("a.selectListTitle")
    print(f"a.selectListTitle -> найдено ссылок: {len(links_selectlisttitle)}")

    links_viewbbs = soup.select('a[href*="viewBbs.do"]')
    print(f'a[href*="viewBbs.do"] -> найдено ссылок: {len(links_viewbbs)}')

    print("\n--- Все найденные <table> на странице (классы) ---")
    for i, t in enumerate(soup.find_all("table")):
        print(f"  table #{i}: class={t.get('class')}")

    print("\n--- Первые 3000 символов HTML (для быстрого просмотра глазами) ---")
    print(html[:3000])


if __name__ == "__main__":
    main()
