import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.llm.base import (
    CorreoGenerado,
    LLMErrorPermanente,
    LLMErrorTransitorio,
    LLMRespuestaInvalida,
)
from app.models import MAX_LARGO_TEXTO, EstadoLead, Lead, utc_now
from app.worker import procesar_uno, reclamar_lead, recuperar_atascados
from tests.fakes import CORREO_OK, FakeLLM

pytestmark = pytest.mark.anyio


async def crear_lead(sesiones, **campos) -> Lead:
    async with sesiones() as session:
        lead = Lead(nombre_empresa="Acme", dominio="acme.com", cargo_contacto="CTO", **campos)
        session.add(lead)
        await session.commit()
        return lead


async def leer(sesiones, lead_id) -> Lead:
    async with sesiones() as session:
        return await session.get(Lead, lead_id)


async def test_pendiente_pasa_a_procesado_con_el_correo(sesiones, settings):
    lead = await crear_lead(sesiones)
    llm = FakeLLM(CORREO_OK)

    hubo_trabajo = await procesar_uno(sesiones, llm, settings)

    assert hubo_trabajo is True
    guardado = await leer(sesiones, lead.id)
    assert guardado.estado == EstadoLead.PROCESADO
    assert guardado.correo_asunto == CORREO_OK.asunto
    # El cuerpo es el del LLM más la firma, que agrega el código (no el LLM).
    assert guardado.correo_cuerpo == (
        f"{CORREO_OK.cuerpo}\n\nSaludos,\nLaura Méndez\nNexo Analytics"
    )
    assert guardado.error_motivo is None
    assert guardado.intentos == 1
    assert guardado.updated_at > guardado.created_at
    # El LLM recibe los datos del lead.
    assert llm.llamadas[0].nombre_empresa == "Acme"


async def test_sin_pendientes_no_hace_nada(sesiones, settings):
    llm = FakeLLM(CORREO_OK)

    assert await procesar_uno(sesiones, llm, settings) is False
    assert llm.llamadas == []


async def test_error_permanente_pasa_a_error_sin_reintentar(sesiones, settings):
    lead = await crear_lead(sesiones)
    llm = FakeLLM(LLMErrorPermanente("Groq respondió 401: Invalid API Key"))

    await procesar_uno(sesiones, llm, settings)

    guardado = await leer(sesiones, lead.id)
    assert guardado.estado == EstadoLead.ERROR
    assert guardado.error_motivo == "Error permanente del LLM: Groq respondió 401: Invalid API Key"
    assert guardado.correo_cuerpo is None
    assert len(llm.llamadas) == 1


async def test_respuesta_invalida_se_regenera_y_luego_procesa(sesiones, settings):
    lead = await crear_lead(sesiones)
    llm = FakeLLM(LLMRespuestaInvalida("contiene un marcador sin reemplazar: [Nombre]"), CORREO_OK)

    await procesar_uno(sesiones, llm, settings)

    guardado = await leer(sesiones, lead.id)
    assert guardado.estado == EstadoLead.PROCESADO
    assert len(llm.llamadas) == 2


async def test_respuesta_invalida_persistente_pasa_a_error_tras_3_generaciones(sesiones, settings):
    lead = await crear_lead(sesiones)
    llm = FakeLLM(LLMRespuestaInvalida("contiene un marcador sin reemplazar: [Nombre]"))

    await procesar_uno(sesiones, llm, settings)

    guardado = await leer(sesiones, lead.id)
    assert guardado.estado == EstadoLead.ERROR
    assert guardado.correo_cuerpo is None
    assert guardado.error_motivo == (
        "Respuesta inválida del LLM: contiene un marcador sin reemplazar: [Nombre] "
        "(tras 3 generaciones)"
    )
    assert len(llm.llamadas) == settings.llm_regeneraciones + 1


@pytest.mark.parametrize(
    ("correo", "motivo"),
    [
        # No nombra al lead: no es un correo personalizado para esta empresa.
        (
            CorreoGenerado(asunto="Hola", cuerpo="En Nexo Analytics ayudamos. ¿Hablamos?"),
            "el correo no menciona a Acme",
        ),
        # No nombra a quien escribe: el modelo inventó otra empresa o ninguna.
        (
            CorreoGenerado(asunto="Para Acme", cuerpo="En Datos SA ayudamos a Acme. ¿Hablamos?"),
            "el cuerpo no menciona a Nexo Analytics (quien escribe)",
        ),
    ],
)
async def test_correo_que_no_menciona_al_lead_o_al_remitente_no_se_guarda(
    sesiones, settings, correo, motivo
):
    lead = await crear_lead(sesiones)
    llm = FakeLLM(correo)

    await procesar_uno(sesiones, llm, settings)

    guardado = await leer(sesiones, lead.id)
    assert guardado.estado == EstadoLead.ERROR
    assert guardado.correo_cuerpo is None
    assert motivo in guardado.error_motivo
    assert len(llm.llamadas) == 3


async def test_error_transitorio_se_reintenta_y_luego_procesa(sesiones, settings):
    lead = await crear_lead(sesiones)
    llm = FakeLLM(LLMErrorTransitorio("timeout tras 20s"), CORREO_OK)

    await procesar_uno(sesiones, llm, settings)

    guardado = await leer(sesiones, lead.id)
    assert guardado.estado == EstadoLead.PROCESADO
    assert len(llm.llamadas) == 2
    # Los reintentos ocurren dentro de un mismo reclamo: no cuentan como intentos nuevos.
    assert guardado.intentos == 1


async def test_error_transitorio_persistente_agota_reintentos_y_pasa_a_error(sesiones, settings):
    lead = await crear_lead(sesiones)
    llm = FakeLLM(LLMErrorTransitorio("timeout tras 20s"))

    await procesar_uno(sesiones, llm, settings)

    guardado = await leer(sesiones, lead.id)
    assert guardado.estado == EstadoLead.ERROR
    assert guardado.error_motivo == "LLM no disponible tras 3 reintentos: timeout tras 20s"
    assert len(llm.llamadas) == settings.llm_max_retries + 1


async def test_excepcion_inesperada_no_deja_el_lead_en_procesando(sesiones, settings):
    lead = await crear_lead(sesiones)
    llm = FakeLLM(RuntimeError("x" * 5000))

    await procesar_uno(sesiones, llm, settings)

    guardado = await leer(sesiones, lead.id)
    assert guardado.estado == EstadoLead.ERROR
    assert guardado.error_motivo.startswith("Error inesperado: RuntimeError")
    # Un mensaje enorme se recorta a 255 en vez de hacer fallar el UPDATE.
    assert len(guardado.error_motivo) == MAX_LARGO_TEXTO
    assert guardado.error_motivo.endswith("…")


async def test_procesa_primero_el_lead_mas_antiguo(sesiones, settings):
    viejo = await crear_lead(sesiones, created_at=utc_now() - timedelta(hours=1))
    nuevo = await crear_lead(sesiones)

    await procesar_uno(sesiones, FakeLLM(CORREO_OK), settings)

    assert (await leer(sesiones, viejo.id)).estado == EstadoLead.PROCESADO
    assert (await leer(sesiones, nuevo.id)).estado == EstadoLead.PENDIENTE


async def test_dos_workers_no_reclaman_el_mismo_lead(sesiones):
    await crear_lead(sesiones)
    await crear_lead(sesiones)

    a, b = await asyncio.gather(reclamar_lead(sesiones), reclamar_lead(sesiones))
    c = await reclamar_lead(sesiones)

    assert a is not None and b is not None
    assert a.id != b.id
    assert c is None


async def test_reclamar_marca_procesando_y_suma_un_intento(sesiones):
    lead = await crear_lead(sesiones)

    reclamado = await reclamar_lead(sesiones)

    assert reclamado.id == lead.id
    assert reclamado.intentos == 1
    assert (await leer(sesiones, lead.id)).estado == EstadoLead.PROCESANDO


async def test_no_pisa_el_resultado_si_otro_worker_retomo_el_lead(sesiones, settings):
    lead = await crear_lead(sesiones)

    class LLMLento:
        """Mientras "piensa", la recuperación devuelve el lead y otro worker lo reclama."""

        async def generar_correo(self, datos):
            async with sesiones() as session:
                await session.execute(
                    update(Lead).where(Lead.id == lead.id).values(estado=EstadoLead.PENDIENTE)
                )
                await session.commit()
            await reclamar_lead(sesiones)
            return CORREO_OK

    await procesar_uno(sesiones, LLMLento(), settings)

    guardado = await leer(sesiones, lead.id)
    # Sigue a cargo del segundo reclamo (intentos=2), sin el correo del primero.
    assert guardado.estado == EstadoLead.PROCESANDO
    assert guardado.intentos == 2
    assert guardado.correo_cuerpo is None


async def test_recuperacion_devuelve_a_pendiente_los_atascados(sesiones, settings):
    hace_mucho = utc_now() - timedelta(minutes=settings.worker_stale_minutes + 1)
    atascado = await crear_lead(sesiones)
    reciente = await crear_lead(sesiones)
    async with sesiones() as session:
        await session.execute(
            update(Lead)
            .where(Lead.id == atascado.id)
            .values(estado=EstadoLead.PROCESANDO, intentos=1, updated_at=hace_mucho)
        )
        await session.execute(
            update(Lead)
            .where(Lead.id == reciente.id)
            .values(estado=EstadoLead.PROCESANDO, intentos=1)
        )
        await session.commit()

    await recuperar_atascados(sesiones, settings)

    assert (await leer(sesiones, atascado.id)).estado == EstadoLead.PENDIENTE
    # Uno que lleva poco tiempo puede seguir en proceso: no se toca.
    assert (await leer(sesiones, reciente.id)).estado == EstadoLead.PROCESANDO


async def test_recuperacion_pasa_a_error_si_agoto_los_intentos(sesiones, settings):
    hace_mucho = utc_now() - timedelta(minutes=settings.worker_stale_minutes + 1)
    lead = await crear_lead(sesiones)
    async with sesiones() as session:
        await session.execute(
            update(Lead)
            .where(Lead.id == lead.id)
            .values(
                estado=EstadoLead.PROCESANDO,
                intentos=settings.max_intentos,
                updated_at=hace_mucho,
            )
        )
        await session.commit()

    await recuperar_atascados(sesiones, settings)

    guardado = await leer(sesiones, lead.id)
    assert guardado.estado == EstadoLead.ERROR
    assert "máximo de intentos" in guardado.error_motivo


async def test_la_bd_exige_motivo_en_estado_error(sesiones):
    """El CHECK constraint protege la regla aunque el código tuviera un bug."""
    lead = await crear_lead(sesiones)

    async with sesiones() as session:
        with pytest.raises(Exception, match="ck_leads_error_tiene_motivo"):
            await session.execute(
                update(Lead).where(Lead.id == lead.id).values(estado=EstadoLead.ERROR)
            )

    async with sesiones() as session:
        assert (await session.scalar(select(Lead.estado))) == EstadoLead.PENDIENTE
