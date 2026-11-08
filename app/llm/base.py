import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field, field_validator

# El prompt pide como máximo 100 palabras; validamos 120 para dar un pequeño margen
# y no descartar un buen correo por unas pocas palabras de más.
MAX_PALABRAS_CUERPO = 120

MAX_LARGO_ASUNTO = 120

# Marcadores sin reemplazar: texto corto entre [], {} o <>, p. ej. "[Nombre]" o "{empresa}",
# y las formas sin corchetes que vimos en respuestas reales ("Tu Empresa", "XXX").
# Distingue mayúsculas: "Tu Empresa" es un marcador, "tu empresa" es texto normal.
MARCADOR_RE = re.compile(
    r"\[[^\]\n]{1,40}\]|\{[^}\n]{1,40}\}|<[^>\n]{1,40}>|\b(?:Tu|Su|Mi) (?:Empresa|Nombre|Cargo)\b"
    r"|\bX{3,}\b"
)

# Última línea que es solo una despedida ("Un saludo,", "Saludos", "Atentamente,"):
# el modelo no debe despedirse porque la firma la agrega el código.
DESPEDIDA_FINAL_RE = re.compile(
    r"(?:^|\n)\s*(?:un\s+)?(?:saludos?|cordiales\s+saludos|atentamente|cordialmente|"
    r"un\s+abrazo|gracias)[\s,.!]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DatosLead:
    """Lo único que el LLM necesita saber del lead. Desacopla la capa LLM del modelo ORM."""

    nombre_empresa: str
    dominio: str
    cargo_contacto: str


class CorreoGenerado(BaseModel):
    """Formato que exigimos al LLM: {"asunto": str, "cuerpo": str}."""

    asunto: str = Field(min_length=1, max_length=MAX_LARGO_ASUNTO)
    cuerpo: str = Field(min_length=1)

    @field_validator("asunto", "cuerpo")
    @classmethod
    def sin_espacios_sobrantes(cls, texto: str) -> str:
        return texto.strip()

    @field_validator("cuerpo")
    @classmethod
    def validar_largo_cuerpo(cls, cuerpo: str) -> str:
        palabras = len(cuerpo.split())
        if palabras > MAX_PALABRAS_CUERPO:
            raise ValueError(f"el cuerpo tiene {palabras} palabras (máximo {MAX_PALABRAS_CUERPO})")
        return cuerpo

    @field_validator("cuerpo")
    @classmethod
    def sin_despedida_final(cls, cuerpo: str) -> str:
        if DESPEDIDA_FINAL_RE.search(cuerpo):
            raise ValueError("termina con una despedida; la firma la agrega el sistema")
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


# Palabras que no identifican a una empresa: "Grupo Bimbo S.A. de C.V." se reconoce por "bimbo".
PALABRAS_GENERICAS = {
    "grupo",
    "group",
    "de",
    "del",
    "la",
    "el",
    "the",
    "and",
    "y",
    "s.a.",
    "sa",
    "s.a",
    "c.v.",
    "cv",
    "sas",
    "spa",
    "ltda",
    "inc",
    "inc.",
    "llc",
    "corp",
}


def _menciona(texto: str, empresa: str) -> bool:
    """True si el texto nombra a la empresa (completa o alguna palabra que la identifique)."""
    texto = texto.casefold()
    if empresa.casefold() in texto:
        return True
    palabras = [p for p in empresa.casefold().split() if p not in PALABRAS_GENERICAS and len(p) > 2]
    return any(re.search(rf"\b{re.escape(p)}\b", texto) for p in palabras)


def validar_contenido(correo: CorreoGenerado, lead: DatosLead, empresa_remitente: str) -> None:
    """Chequeos que dependen del lead: el correo debe ser para ESTA empresa y de NUESTRA empresa."""
    if not _menciona(f"{correo.asunto}\n{correo.cuerpo}", lead.nombre_empresa):
        raise LLMRespuestaInvalida(f"el correo no menciona a {lead.nombre_empresa}")
    if not _menciona(correo.cuerpo, empresa_remitente):
        raise LLMRespuestaInvalida(f"el cuerpo no menciona a {empresa_remitente} (quien escribe)")


class LLMClient(Protocol):
    """Cualquier objeto con este método sirve como cliente: el real (Groq) o uno falso en tests."""

    async def generar_correo(self, lead: DatosLead) -> CorreoGenerado: ...
