"""Worker que genera los correos de los leads pendientes.

Flujo por lead, en tres pasos para no mantener un lock de BD durante la llamada al LLM:
  1. Transacción corta: reclamar un lead pendiente (FOR UPDATE SKIP LOCKED) -> "procesando".
  2. Llamar al LLM fuera de cualquier transacción.
  3. Transacción corta: guardar "procesado" o "error", solo si el lead sigue siendo nuestro.

Se ejecuta como proceso aparte (python -m app.worker) o embebido en la API (RUN_WORKER_IN_API).
"""

import asyncio
import contextlib
import logging
import signal
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings, get_settings
from app.db import SessionLocal, engine
from app.llm.base import (
    CorreoGenerado,
    DatosLead,
    LLMClient,
    LLMErrorPermanente,
    LLMErrorTransitorio,
    LLMRespuestaInvalida,
    validar_contenido,
)
from app.llm.groq_client import GroqClient
from app.llm.prompt import agregar_firma
from app.logs import configurar_logging
from app.models import EstadoLead, Lead, truncar, utc_now

logger = logging.getLogger(__name__)

Sesiones = async_sessionmaker[AsyncSession]

# Tope de espera entre reintentos del LLM, aunque el proveedor pida más.
MAX_ESPERA_SEGUNDOS = 30


@dataclass(frozen=True)
class LeadReclamado:
    id: uuid.UUID
    # Valor de "intentos" al reclamarlo: sirve como versión para el guardado condicional.
    intentos: int
    datos: DatosLead


async def recuperar_atascados(sesiones: Sesiones, settings: Settings) -> None:
    """Devuelve a "pendiente" los leads que llevan demasiado tiempo en "procesando".

    Pasa si un worker se cayó a mitad de un lead. Si el lead ya agotó sus intentos, queda en
    "error" para no reintentar para siempre un lead que tumba al worker.
    """
    limite = utc_now() - timedelta(minutes=settings.worker_stale_minutes)
    atascado = (Lead.estado == EstadoLead.PROCESANDO) & (Lead.updated_at < limite)

    async with sesiones() as session, session.begin():
        agotados = await session.execute(
            update(Lead)
            .where(atascado, Lead.intentos >= settings.max_intentos)
            .values(
                estado=EstadoLead.ERROR,
                error_motivo=f"Se interrumpió el procesamiento {settings.max_intentos} veces "
                "(máximo de intentos alcanzado)",
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        devueltos = await session.execute(
            update(Lead)
            .where(atascado, Lead.intentos < settings.max_intentos)
            .values(estado=EstadoLead.PENDIENTE, updated_at=utc_now())
            .execution_options(synchronize_session=False)
        )

    if agotados.rowcount or devueltos.rowcount:
        logger.warning(
            "Recuperación: %d leads devueltos a pendiente, %d pasados a error",
            devueltos.rowcount,
            agotados.rowcount,
        )


async def reclamar_lead(sesiones: Sesiones) -> LeadReclamado | None:
    """Paso 1: toma el lead pendiente más antiguo y lo marca "procesando" (y hace commit)."""
    async with sesiones() as session, session.begin():
        consulta = (
            select(Lead)
            .where(Lead.estado == EstadoLead.PENDIENTE)
            .order_by(Lead.created_at)
            .limit(1)
            # SKIP LOCKED: si otro worker ya bloqueó esa fila, la salta en vez de esperar.
            # Así varios workers pueden correr a la vez sin tomar el mismo lead.
            .with_for_update(skip_locked=True)
        )
        lead = await session.scalar(consulta)
        if lead is None:
            return None

        lead.estado = EstadoLead.PROCESANDO
        lead.intentos += 1
        # Al salir del bloque session.begin() se hace commit y se libera el lock.
        return LeadReclamado(
            id=lead.id,
            intentos=lead.intentos,
            datos=DatosLead(lead.nombre_empresa, lead.dominio, lead.cargo_contacto),
        )


async def generar_con_reintentos(
    llm: LLMClient, datos: DatosLead, settings: Settings
) -> CorreoGenerado:
    """Paso 2: llama al LLM reintentando con backoff exponencial SOLO los errores transitorios."""
    backoff = wait_exponential(multiplier=settings.llm_backoff_seconds, max=MAX_ESPERA_SEGUNDOS)

    def calcular_espera(estado: RetryCallState) -> float:
        # Si el proveedor dijo cuánto esperar (Retry-After en un 429), se respeta ese valor;
        # el tope evita que un lead se quede tanto tiempo que la recuperación lo dé por atascado.
        error = estado.outcome.exception() if estado.outcome else None
        sugerida = getattr(error, "reintentar_en", None) or 0
        return min(max(backoff(estado), sugerida), MAX_ESPERA_SEGUNDOS)

    reintentar = AsyncRetrying(
        retry=retry_if_exception_type(LLMErrorTransitorio),
        stop=stop_after_attempt(settings.llm_max_retries + 1),
        wait=calcular_espera,
        before_sleep=before_sleep_log(logger, logging.WARNING),
        # Al agotar los intentos relanza el último error original (y no un RetryError).
        reraise=True,
    )
    return await reintentar(llm.generar_correo, datos)


async def generar_correo_valido(
    llm: LLMClient, datos: DatosLead, settings: Settings
) -> CorreoGenerado:
    """Pide el correo y lo valida contra el lead; si es inválido, pide otro (regenera).

    Dos niveles distintos: generar_con_reintentos repite la MISMA petición ante fallas de red;
    este bucle pide un correo NUEVO cuando el anterior no pasó la validación.
    Un correo inválido nunca se devuelve: o sale uno válido o se lanza LLMRespuestaInvalida.
    """
    total = settings.llm_regeneraciones + 1
    for generacion in range(1, total + 1):
        try:
            correo = await generar_con_reintentos(llm, datos, settings)
            validar_contenido(correo, datos, settings.remitente_empresa)
            return correo
        except LLMRespuestaInvalida as exc:
            if generacion == total:
                raise LLMRespuestaInvalida(f"{exc} (tras {total} generaciones)") from exc
            logger.warning("Correo inválido (%s); generando otro (%d/%d)", exc, generacion, total)
    raise AssertionError("inalcanzable")


async def guardar_resultado(sesiones: Sesiones, reclamado: LeadReclamado, **valores) -> bool:
    """Paso 3: guarda el resultado solo si el lead sigue "procesando" con nuestro mismo intento.

    Si mientras llamábamos al LLM la recuperación lo devolvió a "pendiente" y otro worker lo
    tomó, "intentos" ya no coincide: no pisamos su resultado y descartamos el nuestro.
    """
    async with sesiones() as session, session.begin():
        resultado = await session.execute(
            update(Lead)
            .where(
                Lead.id == reclamado.id,
                Lead.estado == EstadoLead.PROCESANDO,
                Lead.intentos == reclamado.intentos,
            )
            .values(**valores, updated_at=utc_now())
            .execution_options(synchronize_session=False)
        )
    if resultado.rowcount == 0:
        logger.warning("Lead %s ya no estaba a nuestro cargo; resultado descartado", reclamado.id)
        return False
    return True


async def procesar_uno(sesiones: Sesiones, llm: LLMClient, settings: Settings) -> bool:
    """Procesa un lead pendiente. Devuelve False si no había ninguno."""
    reclamado = await reclamar_lead(sesiones)
    if reclamado is None:
        return False

    try:
        correo = await generar_correo_valido(llm, reclamado.datos, settings)
    except LLMErrorTransitorio as exc:
        motivo = f"LLM no disponible tras {settings.llm_max_retries} reintentos: {exc}"
    except LLMRespuestaInvalida as exc:
        motivo = f"Respuesta inválida del LLM: {exc}"
    except LLMErrorPermanente as exc:
        motivo = f"Error permanente del LLM: {exc}"
    except Exception as exc:
        # Cualquier otro fallo también termina en "error" con motivo, en vez de dejar
        # el lead colgado en "procesando" hasta que lo rescate la recuperación.
        logger.exception("Error inesperado procesando el lead %s", reclamado.id)
        motivo = f"Error inesperado: {type(exc).__name__}: {exc}"
    else:
        await guardar_resultado(
            sesiones,
            reclamado,
            estado=EstadoLead.PROCESADO,
            correo_asunto=correo.asunto,
            correo_cuerpo=agregar_firma(correo.cuerpo, settings),
            error_motivo=None,
        )
        logger.info("Lead %s procesado", reclamado.id)
        return True

    # La columna ya recorta sola (TextoTruncado); recortar aquí hace que el log diga lo mismo.
    motivo = truncar(motivo)
    await guardar_resultado(sesiones, reclamado, estado=EstadoLead.ERROR, error_motivo=motivo)
    logger.warning("Lead %s en error: %s", reclamado.id, motivo)
    return True


async def run_forever(
    sesiones: Sesiones, llm: LLMClient, settings: Settings, detener: asyncio.Event
) -> None:
    """Bucle principal. Es el mismo en modo proceso aparte y en modo embebido en la API."""
    logger.info("Worker iniciado")
    while not detener.is_set():
        try:
            await recuperar_atascados(sesiones, settings)
            hubo_trabajo = await procesar_uno(sesiones, llm, settings)
        except Exception:
            # Ej.: la BD se cayó un momento. Se registra y se sigue; el worker no debe morir.
            logger.exception("Error en el ciclo del worker")
            hubo_trabajo = False

        if not hubo_trabajo:
            # Espera el intervalo de polling, pero despierta enseguida si piden detenerlo.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    detener.wait(), timeout=settings.worker_poll_interval_seconds
                )
    logger.info("Worker detenido")


def validar_config(settings: Settings) -> None:
    """Falla al arrancar (y no lead por lead) si falta configuración imprescindible."""
    if not settings.groq_api_key.get_secret_value():
        raise SystemExit("Falta GROQ_API_KEY: el worker no puede generar correos sin ella.")


async def main() -> None:
    settings = get_settings()
    configurar_logging(settings.log_level)
    validar_config(settings)

    # Apagado limpio con `docker compose stop` (SIGTERM) o Ctrl+C: termina el lead en curso.
    detener = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows no soporta add_signal_handler
            loop.add_signal_handler(sig, detener.set)

    try:
        async with GroqClient(settings) as llm:
            await run_forever(SessionLocal, llm, settings, detener)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
