from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core import settings

engine = create_engine(
    settings().effective_database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    with SessionLocal() as session:
        yield session
