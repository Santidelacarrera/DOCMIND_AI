from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core import settings

_url = settings().effective_database_url
_pool_options: dict[str, object] = (
    {}
    if _url.startswith("sqlite")
    else {"pool_size": 10, "max_overflow": 10, "pool_timeout": 30, "pool_recycle": 1800}
)
engine = create_engine(_url, pool_pre_ping=True, **_pool_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
