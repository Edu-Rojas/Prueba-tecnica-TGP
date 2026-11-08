from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Lee variables de entorno (sin distinguir mayúsculas) y, si existe, el archivo .env.
    # Las variables de entorno reales tienen prioridad sobre el .env.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://leads:leads@localhost:5432/leads"

    # SecretStr evita que la key aparezca si alguien imprime o loguea la config.
    # Vacía por defecto: la API no la necesita, solo el worker la valida al arrancar.
    groq_api_key: SecretStr = SecretStr("")
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_timeout_seconds: float = 20.0
    llm_max_retries: int = 3

    worker_poll_interval_seconds: float = 2.0
    worker_stale_minutes: int = 5
    max_intentos: int = 3
    run_worker_in_api: bool = False

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    # Cacheada para leer el entorno una sola vez; en tests se puede limpiar con cache_clear().
    return Settings()
