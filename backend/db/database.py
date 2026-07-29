import asyncio
import os
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from urllib.parse import urlparse, parse_qs, urlencode

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine, AsyncEngine
from psycopg.errors import OperationalError as Psycopg3OperationalError
from sqlalchemy.exc import OperationalError as SqlalchemyOperationalError
from sqlalchemy.orm import DeclarativeBase

from config import POSTGRES_URI, POSTGRES_SCHEMA

logger = structlog.get_logger()

# Per-pod cap on how many *tool-execution* DB sessions may be checked out at
# once. Parallel tool fan-out (chat + workflow both `asyncio.gather` a fresh
# session per tool call) is the single largest concurrent consumer of the
# 40-connection pool (pool_size=15 + max_overflow=25). With no cap, one
# many-tool round — or several overlapping turns, more likely now that Kimi's
# slower responses keep turns in-flight longer — can drive gideon toward 40
# connections and, combined with other services on the shared Postgres, hit
# the server's global `max_connections` ("remaining connection slots are
# reserved"). Bounding tool sessions here leaves headroom in the pool for
# request handlers and the background schedulers. Tools still run in parallel,
# just at most TOOL_DB_CONCURRENCY at a time per pod. Created at import (no
# running loop yet) — asyncio.Semaphore binds to the loop lazily on first use.
TOOL_DB_CONCURRENCY = int(os.environ.get("TOOL_DB_CONCURRENCY", "10"))
TOOL_DB_SEMAPHORE = asyncio.Semaphore(TOOL_DB_CONCURRENCY)

CONNECT_ARGS = {"options": f"-c search_path={POSTGRES_SCHEMA}"}
parsed_uri = urlparse(POSTGRES_URI)
query_params = parse_qs(parsed_uri.query)
query_params["sslmode"] = "disable"

new_query = urlencode(query_params, doseq=True)
DB_URI = parsed_uri._replace(query=new_query).geturl()

ENGINE_ASYNC = create_async_engine(
    DB_URI,
    pool_pre_ping=True,
    pool_size=15,
    max_overflow=25,
    future=True,
    pool_use_lifo=True,
    connect_args=CONNECT_ARGS,
)


class Base(DeclarativeBase):
    pass


class AsyncPostgresSessionManager(ABC):
    def __init__(self):
        self._session_factory = async_sessionmaker(
            bind=self.engine, class_=AsyncSession, expire_on_commit=False
        )

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        session: AsyncSession = self._session_factory()
        try:
            yield session
            await session.commit()
        except BaseException:
            # Catch BaseException (not Exception) so asyncio.CancelledError also
            # rolls back. Without this, a client disconnect mid-request skips
            # rollback, returns the connection to the pool with an active
            # transaction, and the next checkout fails with
            # "can't change autocommit now: connection in transaction status ACTIVE".
            logger.exception("Rolling back session due to exception")
            try:
                await session.rollback()
            except Exception:
                logger.exception("Rollback failed during session teardown")
            raise
        finally:
            await session.close()

    @property
    @abstractmethod
    def engine(self) -> AsyncEngine:
        raise NotImplementedError


class AsyncPostgresSessionGideon(AsyncPostgresSessionManager):
    @property
    def engine(self):
        return ENGINE_ASYNC


def retry_on_db_operational_error(f):
    async def wrapper(*args, **kw):
        tries = 0
        last_error = None
        while tries < 3:
            tries += 1
            try:
                return await f(*args, **kw)
            except (Psycopg3OperationalError, SqlalchemyOperationalError) as e:
                logger.exception("DB OperationalError, retrying", attempt=tries)
                last_error = e
                await asyncio.sleep(0.5 * tries)
                continue

        if last_error:
            logger.exception("DB retries exhausted", error=str(last_error))
            raise last_error

        raise Exception("Max retries exceeded for async db operation")

    return wrapper


session_manager = AsyncPostgresSessionGideon()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with session_manager.get_session() as session:
        yield session
