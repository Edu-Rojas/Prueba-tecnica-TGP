"""RUN_WORKER_IN_API: el worker corre dentro de la API como tarea async del lifespan."""

import asyncio
import time

import httpx
import pytest

import app.main as main
from tests.fakes import CORREO_OK, FakeLLM

pytestmark = pytest.mark.anyio

LEAD = {"nombre_empresa": "Acme", "dominio": "acme.com", "cargo_contacto": "CTO"}


class LLMLento(FakeLLM):
    async def generar_correo(self, lead):
        await asyncio.sleep(0.5)
        return await super().generar_correo(lead)


def configurar(monkeypatch, settings, *, embebido: bool, llm: FakeLLM) -> None:
    monkeypatch.setattr(
        main,
        "settings",
        settings.model_copy(
            update={"run_worker_in_api": embebido, "worker_poll_interval_seconds": 0.05}
        ),
    )
    monkeypatch.setattr(main, "crear_llm", lambda _settings: llm)


async def estado_del_lead(cliente: httpx.AsyncClient) -> str:
    return (await cliente.get("/leads")).json()[0]["estado"]


async def test_worker_embebido_procesa_leads_sin_bloquear_la_api(sesiones, settings, monkeypatch):
    llm = LLMLento(CORREO_OK)
    configurar(monkeypatch, settings, embebido=True, llm=llm)
    transporte = httpx.ASGITransport(app=main.app)

    async with main.lifespan(main.app):
        async with httpx.AsyncClient(transport=transporte, base_url="http://test") as cliente:
            assert (await cliente.post("/leads", json=LEAD)).status_code == 202

            # Mientras el worker espera al LLM (0,5 s), la API sigue respondiendo al instante.
            await asyncio.sleep(0.2)
            inicio = time.perf_counter()
            assert await estado_del_lead(cliente) == "procesando"
            assert time.perf_counter() - inicio < 0.3

            for _ in range(60):
                if await estado_del_lead(cliente) == "procesado":
                    break
                await asyncio.sleep(0.05)
            assert await estado_del_lead(cliente) == "procesado"

    assert len(llm.llamadas) == 1


async def test_sin_la_variable_la_api_no_procesa_leads(sesiones, settings, monkeypatch):
    llm = FakeLLM(CORREO_OK)
    configurar(monkeypatch, settings, embebido=False, llm=llm)
    transporte = httpx.ASGITransport(app=main.app)

    async with main.lifespan(main.app):
        async with httpx.AsyncClient(transport=transporte, base_url="http://test") as cliente:
            await cliente.post("/leads", json=LEAD)
            await asyncio.sleep(0.3)
            assert await estado_del_lead(cliente) == "pendiente"

    assert llm.llamadas == []
