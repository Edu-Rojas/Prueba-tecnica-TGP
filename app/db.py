from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

engine = create_async_engine(
    get_settings().database_url,
    # Descarta conexiones muertas (p. ej. después de que Render duerme el servicio).
    pool_pre_ping=True,
    # La sesión de Postgres trabaja en UTC: now(), comparaciones y casts usan la misma zona.
    connect_args={"server_settings": {"timezone": "UTC"}},
)

# expire_on_commit=False: en async no se puede recargar un atributo de forma implícita
# (lazy load), así que los objetos tienen que seguir legibles después del commit.
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependencia de FastAPI: una sesión por request, cerrada siempre al terminar."""
    async with SessionLocal() as session:
        yield session
