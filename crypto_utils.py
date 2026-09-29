"""
crypto_utils.py
----------------
Шифрование секретов (пароль портала, app password почты) перед записью в БД.

ПЕРЕД ПЕРВЫМ ЗАПУСКОМ сгенерируй ключ ОДИН раз:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Полученную строку положи в .env как:

    SECRETS_ENCRYPTION_KEY=тут_твой_ключ

И добавь в свой env.py (там же, где API_ID, BOT_TOKEN и т.д.):

    SECRETS_ENCRYPTION_KEY = os.environ["SECRETS_ENCRYPTION_KEY"]

ВАЖНО: потеряешь этот ключ — расшифровать сохранённые пароли будет
невозможно, придётся всем юзерам заново вводить креды. Храни ключ
отдельно от базы данных (например, в .env сервера, не в самой БД).
"""

import os
from cryptography.fernet import Fernet

_key = os.environ.get("SECRETS_ENCRYPTION_KEY")
if not _key:
    raise RuntimeError(
        "SECRETS_ENCRYPTION_KEY не задан. Сгенерируй ключ командой:\n"
        "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
        "и положи результат в .env как SECRETS_ENCRYPTION_KEY=..."
    )

_fernet = Fernet(_key.encode() if isinstance(_key, str) else _key)


def encrypt(value: str) -> str | None:
    if value is None:
        return None
    return _fernet.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt(value: str) -> str | None:
    if value is None:
        return None
    return _fernet.decrypt(value.encode("utf-8")).decode("utf-8")
