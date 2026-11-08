"""create leads table

Revision ID: 2b23232b2c5f
Revises:
Create Date: 2026-11-07 23:51:43.613064

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "2b23232b2c5f"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

estado_lead = sa.Enum("pendiente", "procesando", "procesado", "error", name="estado_lead")


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("nombre_empresa", sa.String(length=200), nullable=False),
        sa.Column("dominio", sa.String(length=253), nullable=False),
        sa.Column("cargo_contacto", sa.String(length=200), nullable=False),
        sa.Column("estado", estado_lead, server_default="pendiente", nullable=False),
        sa.Column("correo_asunto", sa.String(length=300), nullable=True),
        sa.Column("correo_cuerpo", sa.Text(), nullable=True),
        sa.Column("error_motivo", sa.Text(), nullable=True),
        sa.Column("intentos", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "estado <> 'error' OR error_motivo IS NOT NULL",
            name=op.f("ck_leads_error_tiene_motivo"),
        ),
        sa.CheckConstraint(
            "estado <> 'procesado' OR (correo_asunto IS NOT NULL AND correo_cuerpo IS NOT NULL)",
            name=op.f("ck_leads_procesado_tiene_correo"),
        ),
        sa.CheckConstraint("intentos >= 0", name=op.f("ck_leads_intentos_no_negativos")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leads")),
    )
    op.create_index("ix_leads_estado_created_at", "leads", ["estado", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_leads_estado_created_at", table_name="leads")
    op.drop_table("leads")
    # drop_table no borra el tipo ENUM de Postgres; sin esto, un nuevo upgrade fallaría
    # con "type estado_lead already exists".
    estado_lead.drop(op.get_bind(), checkfirst=True)
