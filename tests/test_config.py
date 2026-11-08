import pytest

from app.config import Settings


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        # Formato que entrega Render.
        ("postgresql://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
        ("postgres://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"),
        # Ya con driver: no se toca.
        ("postgresql+asyncpg://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
    ],
)
def test_database_url_usa_siempre_asyncpg(entrada, esperado):
    assert Settings(database_url=entrada).database_url == esperado


def test_la_api_key_no_aparece_al_imprimir_la_config():
    settings = Settings(groq_api_key="gsk_super_secreta")

    assert "gsk_super_secreta" not in repr(settings)
    assert "gsk_super_secreta" not in str(settings.model_dump())
