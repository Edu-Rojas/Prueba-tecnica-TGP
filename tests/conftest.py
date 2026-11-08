"""Configuración de los tests: Postgres real (BD aparte "<nombre>_test") y LLM siempre falso.

Las variables de entorno se fijan ANTES de que se importe cualquier módulo que cree el engine
o lea la config cacheada, por eso aquí solo se importa app.config arriba.
"""

import asyncio
import os

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.config import Settings


def _url_de_test() -> str:
    if url := os.environ.get("TEST_DATABASE_URL"):
        return url
    # Misma BD que la app pero con sufijo _test: los tests nunca tocan los datos de desarrollo.
    base = make_url(os.environ.get("DATABASE_URL") or Settings().database_url)
    return base.set(database=f"{base.database}_test").render_as_string(hide_password=False)


URL_TEST = _url_de_test()
os.environ["DATABASE_URL"] = URL_TEST
os.environ["RUN_WORKER_IN_API"] = "false"
# Ningún test puede usar la key real de Groq, aunque exista en el .env.
os.environ["GROQ_API_KEY"] = ""


async def _crear_bd_si_no_existe() -> None:
    url = make_url(URL_TEST)
    # CREATE DATABASE no puede ir dentro de una transacción: AUTOCOMMIT, conectados a "postgres".
    engine = create_async_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with engine.connect() as conn:
        existe = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :nombre"), {"nombre": url.database}
        )
        if not existe:
            await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def esquema_migrado() -> None:
    """Crea la BD de test y aplica las migraciones reales (también prueba el downgrade)."""
    asyncio.run(_crear_bd_si_no_existe())
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.fixture
def anyio_backend() -> str:
    # Los tests async corren con el plugin de anyio (ya viene con FastAPI/Starlette).
    return "asyncio"


@pytest.fixture
async def sesiones():
    # NullPool: cada test usa su propio event loop y las conexiones de asyncpg no pueden
    # pasar de un loop a otro, así que no se reutilizan conexiones entre tests.
    engine = create_async_engine(URL_TEST, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE leads"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    # Sin esperas entre reintentos para que los tests sean rápidos.
    return Settings(llm_backoff_seconds=0, groq_api_key="clave-de-test")


@pytest.fixture
async def cliente(sesiones):
    """Cliente HTTP contra la app real, con get_session apuntando a la BD de test."""
    from app.db import get_session
    from app.main import app

    async def get_session_de_test():
        async with sesiones() as session:
            yield session

    app.dependency_overrides[get_session] = get_session_de_test
    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transporte, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
