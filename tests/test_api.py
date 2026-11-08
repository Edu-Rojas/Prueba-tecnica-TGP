import pytest
from sqlalchemy import select

from app.llm.groq_client import GroqClient
from app.models import EstadoLead, Lead

pytestmark = pytest.mark.anyio

LEAD = {"nombre_empresa": "Acme", "dominio": "acme.com", "cargo_contacto": "CTO"}


async def test_post_guarda_pendiente_y_no_llama_al_llm(cliente, sesiones, monkeypatch):
    async def no_debe_llamarse(*args, **kwargs):
        raise AssertionError("POST /leads no debe llamar al LLM")

    monkeypatch.setattr(GroqClient, "generar_correo", no_debe_llamarse)

    respuesta = await cliente.post("/leads", json=LEAD)

    assert respuesta.status_code == 202
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "pendiente"
    assert cuerpo["correo_asunto"] is None
    assert cuerpo["intentos"] == 0
    async with sesiones() as session:
        lead = await session.scalar(select(Lead))
    assert lead.estado == EstadoLead.PENDIENTE
    assert str(lead.id) == cuerpo["id"]


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("acme.com", "acme.com"),
        ("  ACME.com  ", "acme.com"),
        ("https://www.acme.com/contacto?x=1", "acme.com"),
        ("http://sub.acme.co.uk/", "sub.acme.co.uk"),
        ("www.acme.com.", "acme.com"),
    ],
)
async def test_post_normaliza_el_dominio(cliente, entrada, esperado):
    respuesta = await cliente.post("/leads", json={**LEAD, "dominio": entrada})

    assert respuesta.status_code == 202
    assert respuesta.json()["dominio"] == esperado


@pytest.mark.parametrize(
    "cambios",
    [
        {"dominio": "no es un dominio"},
        {"dominio": "acme"},
        {"dominio": "-acme.com"},
        {"nombre_empresa": "   "},
        {"cargo_contacto": ""},
        {"nombre_empresa": "a" * 256},
    ],
)
async def test_post_rechaza_datos_invalidos(cliente, sesiones, cambios):
    respuesta = await cliente.post("/leads", json={**LEAD, **cambios})

    assert respuesta.status_code == 422
    async with sesiones() as session:
        assert await session.scalar(select(Lead)) is None


async def test_post_rechaza_campos_faltantes(cliente):
    respuesta = await cliente.post("/leads", json={"nombre_empresa": "Acme"})

    assert respuesta.status_code == 422


async def test_get_lista_con_correo_y_mas_recientes_primero(cliente, sesiones):
    async with sesiones() as session:
        viejo = Lead(**LEAD)
        session.add(viejo)
        await session.commit()
        nuevo = Lead(
            **{**LEAD, "nombre_empresa": "Globex"},
            estado=EstadoLead.PROCESADO,
            correo_asunto="Asunto",
            correo_cuerpo="Cuerpo",
        )
        session.add(nuevo)
        await session.commit()

    respuesta = await cliente.get("/leads")

    assert respuesta.status_code == 200
    leads = respuesta.json()
    assert [lead["nombre_empresa"] for lead in leads] == ["Globex", "Acme"]
    assert leads[0]["estado"] == "procesado"
    assert leads[0]["correo_asunto"] == "Asunto"
    assert leads[0]["correo_cuerpo"] == "Cuerpo"
    assert leads[1]["correo_asunto"] is None
    # Fechas en UTC con zona explícita.
    assert leads[0]["created_at"].endswith("Z")


async def test_get_pagina_con_limit_y_offset(cliente):
    for nombre in ["A", "B", "C"]:
        await cliente.post("/leads", json={**LEAD, "nombre_empresa": nombre})

    pagina = await cliente.get("/leads", params={"limit": 1, "offset": 1})

    assert [lead["nombre_empresa"] for lead in pagina.json()] == ["B"]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
async def test_get_rechaza_paginacion_invalida(cliente, params):
    respuesta = await cliente.get("/leads", params=params)

    assert respuesta.status_code == 422


async def test_health(cliente):
    respuesta = await cliente.get("/health")

    assert respuesta.json() == {"status": "ok"}
