from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os
from env import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"sslmode": "require"}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)

Base = declarative_base()  # ← this was missing