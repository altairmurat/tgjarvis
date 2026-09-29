from sqlalchemy import Column, Integer, Float, String, ForeignKey, Text, BigInteger
from database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_user_id = Column(BigInteger, unique=True)
    username = Column(String, nullable=True)

class Contact(Base):
    __tablename__ = "contacts"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    display_name = Column(String)
    email = Column(String)
    source = Column(String)
    confidence = Column(Float, default=1.0)
    
class GoogleAccount(Base):
    __tablename__ = "google_accounts"
    id = Column(Integer, primary_key=True)
    telegram_user_id = Column(BigInteger, unique=True, index=True)
    email = Column(String, nullable=True)          # для показа "подключено как X@gmail.com"
    token_json = Column(Text)                      # весь Credentials.to_json() одной строкой
    
class Communication(Base):
    __tablename__ = "communication"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String)
    usermessage = Column(String)
    
class DgistAccount(Base):
    __tablename__ = "dgist_accounts"

    id = Column(Integer, primary_key=True)
    telegram_user_id = Column(BigInteger, unique=True, index=True)

    dgist_username = Column(String)              # логин от портала — не секрет, не шифруем
    dgist_password_enc = Column(Text)             # ЗАШИФРОВАНО через crypto_utils.encrypt()

    email_address = Column(String)                # почта, куда приходит код — не секрет
    email_app_password_enc = Column(Text)          # ЗАШИФРОВАНО
    email_imap_host = Column(String, default="imap.gmail.com")
