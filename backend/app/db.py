from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core import settings


def _pool_options(url: str, size: int) -> dict[str, object]:
    if url.startswith("sqlite"):
        return {}
    return {"pool_size": size, "max_overflow": size, "pool_timeout": 30, "pool_recycle": 1800}


_url = settings().effective_database_url
engine = create_engine(_url, pool_pre_ping=True, **_pool_options(_url, 10))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# Cross-organization maintenance (beat tasks: recovery, retention, reconciliation, outboxes).
# With RLS phase 3 this is the docmind_system role (BYPASSRLS); otherwise the same database
# and credentials as SessionLocal. Never used on a request path.
_system_url = settings().effective_database_system_url
system_engine = (
    engine if _system_url == _url else create_engine(_system_url, pool_pre_ping=True, **_pool_options(_system_url, 2))
)
SystemSessionLocal = sessionmaker(bind=system_engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _install_rls() -> None:
    from app import rls

    rls.install(SessionLocal)


_install_rls()


def get_db() -> Iterator[Session]:
    from app import rls

    try:
        with SessionLocal() as session:
            yield session
    finally:
        rls.clear_context()  # the organization context never outlives its request
