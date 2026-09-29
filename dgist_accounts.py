"""
dgist_accounts.py
------------------
DB-хелперы для DGIST-кредов, по образу save_user_token/load_user_creds
для Gmail в основном файле бота. Секреты шифруются/расшифровываются
прозрачно через crypto_utils.
"""

from database import SessionLocal
import models
import crypto_utils


def save_dgist_account(
    telegram_user_id: int,
    dgist_username: str,
    dgist_password: str,
    email_address: str,
    email_app_password: str,
    email_imap_host: str = "imap.gmail.com",
) -> None:
    db = SessionLocal()
    try:
        acc = db.query(models.DgistAccount).filter_by(telegram_user_id=telegram_user_id).first()
        if not acc:
            acc = models.DgistAccount(telegram_user_id=telegram_user_id)
            db.add(acc)

        acc.dgist_username = dgist_username
        acc.dgist_password_enc = crypto_utils.encrypt(dgist_password)
        acc.email_address = email_address
        acc.email_app_password_enc = crypto_utils.encrypt(email_app_password)
        acc.email_imap_host = email_imap_host or "imap.gmail.com"
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"save_dgist_account error for {telegram_user_id}: {e}")
        raise
    finally:
        db.close()


def load_dgist_account(telegram_user_id: int) -> dict | None:
    """Возвращает dict с РАСШИФРОВАННЫМИ полями, готовый к передаче в auto_login,
    либо None, если юзер ещё не подключал портал."""
    db = SessionLocal()
    try:
        acc = db.query(models.DgistAccount).filter_by(telegram_user_id=telegram_user_id).first()
        if not acc:
            return None
        return {
            "dgist_username": acc.dgist_username,
            "dgist_password": crypto_utils.decrypt(acc.dgist_password_enc),
            "email_address": acc.email_address,
            "email_app_password": crypto_utils.decrypt(acc.email_app_password_enc),
            "email_imap_host": acc.email_imap_host or "imap.gmail.com",
        }
    except Exception as e:
        print(f"load_dgist_account error for {telegram_user_id}: {e}")
        return None
    finally:
        db.close()


def delete_dgist_account(telegram_user_id: int) -> None:
    db = SessionLocal()
    try:
        acc = db.query(models.DgistAccount).filter_by(telegram_user_id=telegram_user_id).first()
        if acc:
            db.delete(acc)
            db.commit()
    except Exception as e:
        db.rollback()
        print(f"delete_dgist_account error for {telegram_user_id}: {e}")
    finally:
        db.close()


def list_dgist_user_ids() -> list[int]:
    """Возвращает Telegram ID всех пользователей с подключённым DGIST."""
    db = SessionLocal()
    try:
        rows = db.query(models.DgistAccount.telegram_user_id).all()
        return [int(row[0]) for row in rows if row[0] is not None]
    except Exception as e:
        print(f"list_dgist_user_ids error: {e}")
        return []
    finally:
        db.close()
