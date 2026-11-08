import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field, field_validator

# El prompt pide como máximo 100 palabras; validamos 120 para dar un pequeño margen
# y no descartar un buen correo por unas pocas palabras de más.
MAX_PALABRAS_CUERPO = 120

# Texto corto entre corchetes o llaves, p. ej. "[Nombre]" o "{empresa}".
MARCADOR_RE = re.compile(r"\[[^\]\n]{1,40}\]|\{[^}\n]{1,40}\}")


@dataclass(frozen=True)
class DatosLead:
    """Lo único que el LLM necesita saber del lead. Desacopla la capa LLM del modelo ORM."""

    nombre_empresa: str
    dominio: str
    cargo_contacto: str


class CorreoGenerado(BaseModel):
    """Formato que exigimos al LLM: {"asunto": str, "cuerpo": str}."""

    asunto: str = Field(min_length=1, max_length=300)
    cuerpo: str = Field(min_length=1)

    @field_validator("cuerpo")
    @classmethod
    def validar_largo_cuerpo(cls, cuerpo: str) -> str:
        palabras = len(cuerpo.split())
        if palabras > MAX_PALABRAS_CUERPO:
            raise ValueError(f"el cuerpo tiene {palabras} palabras (máximo {MAX_PALABRAS_CUERPO})")
        return cuerpo

    @field_validator("asunto", "cuerpo")
    @classmethod
    def sin_marcadores(cls, texto: str) -> str:
        # El correo se usa tal cual: un "[Nombre]" o "{empresa}" sin reemplazar lo arruina.
        marcador = MARCADOR_RE.search(texto)
        if marcador:
            raise ValueError(f"contiene un marcador sin reemplazar: {marcador.group()}")
        return texto


class LLMError(Exception):
    """Base de los errores del LLM. El mensaje debe ser legible: termina en error_motivo."""


class LLMErrorTransitorio(LLMError):
    """Puede funcionar si se reintenta: timeout, error de red, 429 o 5xx."""

    def __init__(self, mensaje: str, reintentar_en: float | None = None) -> None:
        super().__init__(mensaje)
        # Segundos que el proveedor pide esperar (header Retry-After), si los indicó.
        self.reintentar_en = reintentar_en


class LLMErrorPermanente(LLMError):
    """Reintentar no sirve: API key inválida, modelo inexistente, petición mal formada."""


class LLMRespuestaInvalida(LLMErrorPermanente):
    """El LLM respondió, pero no con el JSON {"asunto", "cuerpo"} esperado."""


class LLMClient(Protocol):
    """Cualquier objeto con este método sirve como cliente: el real (Groq) o uno falso en tests."""

    async def generar_correo(self, lead: DatosLead) -> CorreoGenerado: ...
