from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

# pool_pre_ping: descarta conexiones muertas (p. ej. después de que Render duerme el servicio).
engine = create_engine(get_settings().database_url, pool_pre_ping=True)

# expire_on_commit=False: los objetos siguen legibles después del commit,
# útil para devolver el lead recién creado en la respuesta.
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """Dependencia de FastAPI: una sesión por request, cerrada siempre al terminar."""
    with SessionLocal() as session:
        yield session
