import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Lead
from app.schemas import LeadCreate, LeadOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/leads", tags=["leads"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=LeadOut)
async def crear_lead(datos: LeadCreate, session: SessionDep) -> Lead:
    # 202 y no 201: el lead queda aceptado pero el correo se genera después, en el worker.
    # Este endpoint nunca llama al LLM.
    lead = Lead(**datos.model_dump())
    session.add(lead)
    await session.commit()
    logger.info("Lead %s creado en estado %s", lead.id, lead.estado)
    return lead


@router.get("", response_model=list[LeadOut])
async def listar_leads(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Lead]:
    # Orden estable: a igual created_at desempata por id, para que la paginación no repita filas.
    consulta = (
        select(Lead).order_by(Lead.created_at.desc(), Lead.id.desc()).limit(limit).offset(offset)
    )
    resultado = await session.scalars(consulta)
    return list(resultado)
