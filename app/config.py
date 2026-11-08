from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Lee variables de entorno (sin distinguir mayúsculas) y, si existe, el archivo .env.
    # Las variables de entorno reales tienen prioridad sobre el .env.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://leads:leads@localhost:5432/leads"

    # SecretStr evita que la key aparezca si alguien imprime o loguea la config.
    # Vacía por defecto: la API no la necesita, solo el worker la valida al arrancar.
    groq_api_key: SecretStr = SecretStr("")
    groq_model: str = "qwen/qwen3.8-27b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_timeout_seconds: float = 20.0
    # Reintentos SOLO para errores transitorios; con 3 hay hasta 4 llamadas en total.
    llm_max_retries: int = 3
    # Espera base del backoff exponencial (1s, 2s, 4s...). En tests se pone en 0.
    llm_backoff_seconds: float = 1.0
    # Veces que se pide otro correo si el LLM devuelve uno inválido (2 = hasta 3 generaciones).
    llm_regeneraciones: int = 2

    # Quién envía los correos. Empresa y propuesta van al prompt; la firma la arma el código.
    # Son valores de ejemplo: cambiarlos por los reales en .env o en Render.
    remitente_nombre: str = "Amelia Honorato"
    remitente_empresa: str = "The Growth Pro"
    remitente_propuesta: str = (
        "una plataforma que conecta los datos de ventas, marketing y operaciones en tableros "
        "automáticos, para tomar decisiones con datos sin depender del equipo de TI"
    )

    worker_poll_interval_seconds: float = 2.0
    # Debe ser mayor que lo que puede tardar un lead (timeout x llamadas + esperas),
    # o se "recuperaría" un lead que todavía se está procesando.
    worker_stale_minutes: int = 5
    max_intentos: int = 3
    run_worker_in_api: bool = False

    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def usar_driver_asyncpg(cls, url: str) -> str:
        # Render (y casi todos los proveedores) entregan "postgres://..." o "postgresql://...".
        # SQLAlchemy necesita que se indique el driver async: "postgresql+asyncpg://...".
        for prefijo in ("postgres://", "postgresql://"):
            if url.startswith(prefijo):
                return "postgresql+asyncpg://" + url.removeprefix(prefijo)
        return url


@lru_cache
def get_settings() -> Settings:
    # Cacheada para leer el entorno una sola vez; en tests se puede limpiar con cache_clear().
    return Settings()
