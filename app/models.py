import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, Index, MetaData, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Nombres de constraints predecibles: Alembic puede referenciarlos en migraciones futuras
# en vez de depender de los nombres que Postgres inventa.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class EstadoLead(enum.StrEnum):
    PENDIENTE = "pendiente"
    PROCESANDO = "procesando"
    PROCESADO = "procesado"
    ERROR = "error"


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    nombre_empresa: Mapped[str] = mapped_column(String(200))
    # 253 es el largo máximo de un nombre de dominio.
    dominio: Mapped[str] = mapped_column(String(253))
    cargo_contacto: Mapped[str] = mapped_column(String(200))
    estado: Mapped[EstadoLead] = mapped_column(
        # values_callable: guarda "pendiente" (el valor) y no "PENDIENTE" (el nombre del miembro).
        Enum(EstadoLead, name="estado_lead", values_callable=lambda e: [m.value for m in e]),
        default=EstadoLead.PENDIENTE,
        server_default=EstadoLead.PENDIENTE.value,
    )
    correo_asunto: Mapped[str | None] = mapped_column(String(300))
    correo_cuerpo: Mapped[str | None] = mapped_column(Text)
    error_motivo: Mapped[str | None] = mapped_column(Text)
    intentos: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # El worker busca "los pendientes más antiguos" y GET /leads ordena por fecha.
        Index("ix_leads_estado_created_at", "estado", "created_at"),
        # Reglas de negocio garantizadas por la BD, no solo por el código.
        CheckConstraint("intentos >= 0", name="intentos_no_negativos"),
        CheckConstraint(
            "estado <> 'procesado' OR (correo_asunto IS NOT NULL AND correo_cuerpo IS NOT NULL)",
            name="procesado_tiene_correo",
        ),
        CheckConstraint(
            "estado <> 'error' OR error_motivo IS NOT NULL",
            name="error_tiene_motivo",
        ),
    )
