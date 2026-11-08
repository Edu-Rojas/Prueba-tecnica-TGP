import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import SessionLocal, engine
from app.llm.groq_client import GroqClient
from app.logs import configurar_logging
from app.routes.leads import router as leads_router
from app.worker import run_forever, validar_config

settings = get_settings()
configurar_logging(settings.log_level)
logger = logging.getLogger(__name__)

# Fábrica del cliente LLM del worker embebido; los tests la reemplazan por un cliente falso.
crear_llm = GroqClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if not settings.run_worker_in_api:
        # Modo normal: el worker corre como proceso aparte (servicio "worker" en compose).
        yield
    else:
        # Modo embebido (Render free tier, sin Background Workers): el MISMO run_forever
        # corre como tarea async en el event loop de la API. Como todo es async
        # (asyncpg, httpx), mientras el worker espera al LLM la API sigue atendiendo requests.
        validar_config(settings)
        detener = asyncio.Event()
        async with crear_llm(settings) as llm:
            tarea = asyncio.create_task(
                run_forever(SessionLocal, llm, settings, detener), name="worker-embebido"
            )
            logger.info("Worker embebido en la API (RUN_WORKER_IN_API=true)")
            try:
                yield
            finally:
                # Apagado limpio: el worker termina el lead en curso y sale del bucle.
                detener.set()
                await tarea
    await engine.dispose()


app = FastAPI(title="TGP Leads", version="0.1.0", lifespan=lifespan)
app.include_router(leads_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
