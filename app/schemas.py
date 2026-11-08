import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import MAX_LARGO_TEXTO, EstadoLead

# Uno o más "etiquetas." seguidas de un TLD de letras. Cada etiqueta: 1-63 caracteres,
# letras/números/guiones, sin guion al inicio ni al final. Largo total máximo: 253.
DOMINIO_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class LeadCreate(BaseModel):
    # Quita espacios al inicio y al final de todos los strings antes de validar largos.
    model_config = ConfigDict(str_strip_whitespace=True)

    nombre_empresa: str = Field(min_length=1, max_length=MAX_LARGO_TEXTO, examples=["Acme"])
    dominio: str = Field(min_length=1, max_length=MAX_LARGO_TEXTO, examples=["acme.com"])
    cargo_contacto: str = Field(min_length=1, max_length=MAX_LARGO_TEXTO, examples=["CTO"])

    @field_validator("dominio")
    @classmethod
    def normalizar_dominio(cls, valor: str) -> str:
        # Así "https://www.Acme.com/contacto" y "acme.com" se guardan igual.
        dominio = valor.lower()
        dominio = re.sub(r"^https?://", "", dominio)
        dominio = dominio.split("/")[0]
        dominio = dominio.removeprefix("www.")
        dominio = dominio.rstrip(".")
        if not DOMINIO_RE.fullmatch(dominio):
            raise ValueError("dominio inválido, se espera algo como 'acme.com'")
        return dominio


class LeadOut(BaseModel):
    # from_attributes: permite construirlo directamente desde un objeto Lead de SQLAlchemy.
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    nombre_empresa: str
    dominio: str
    cargo_contacto: str
    estado: EstadoLead
    correo_asunto: str | None
    correo_cuerpo: str | None
    error_motivo: str | None
    intentos: int
    created_at: datetime
    updated_at: datetime
