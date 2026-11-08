"""Cliente real de Groq contra un servidor simulado (httpx.MockTransport): sin red ni key real."""

import json

import httpx
import pytest

from app.llm.base import (
    DatosLead,
    LLMErrorPermanente,
    LLMErrorTransitorio,
    LLMRespuestaInvalida,
)
from app.llm.groq_client import GroqClient
from app.models import EstadoLead, Lead
from app.worker import procesar_uno

pytestmark = pytest.mark.anyio

DATOS = DatosLead("Acme", "acme.com", "CTO")


def respuesta_groq(contenido: str, status: int = 200) -> httpx.Response:
    """Arma una respuesta con la forma de la API de chat de Groq (compatible con OpenAI)."""
    return httpx.Response(status, json={"choices": [{"message": {"content": contenido}}]})


def cliente_con(settings, manejador) -> GroqClient:
    return GroqClient(settings, transport=httpx.MockTransport(manejador))


async def test_respuesta_valida_devuelve_el_correo(settings):
    peticiones: list[httpx.Request] = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        peticiones.append(peticion)
        return respuesta_groq(json.dumps({"asunto": "Hola", "cuerpo": "Correo breve."}))

    async with cliente_con(settings, manejador) as cliente:
        correo = await cliente.generar_correo(DATOS)

    assert correo.asunto == "Hola"
    enviado = json.loads(peticiones[0].content)
    assert enviado["model"] == settings.groq_model
    assert enviado["response_format"] == {"type": "json_object"}
    assert "Acme" in enviado["messages"][1]["content"]
    assert peticiones[0].headers["Authorization"] == "Bearer clave-de-test"


@pytest.mark.parametrize(
    ("contenido", "fragmento_motivo"),
    [
        ("esto no es JSON", "JSON inválido"),
        (json.dumps({"asunto": "Hola"}), "cuerpo"),
        (json.dumps({"asunto": "", "cuerpo": "x"}), "asunto"),
        (json.dumps({"asunto": "Hola", "cuerpo": "palabra " * 121}), "121 palabras"),
        (json.dumps({"asunto": "Hola", "cuerpo": "Hola, [Nombre]: ..."}), "[Nombre]"),
    ],
)
async def test_json_invalido_es_respuesta_invalida(settings, contenido, fragmento_motivo):
    async with cliente_con(settings, lambda _: respuesta_groq(contenido)) as cliente:
        with pytest.raises(LLMRespuestaInvalida, match=fragmento_motivo.replace("[", r"\[")):
            await cliente.generar_correo(DATOS)


async def test_respuesta_sin_choices_es_respuesta_invalida(settings):
    async with cliente_con(settings, lambda _: httpx.Response(200, json={})) as cliente:
        with pytest.raises(LLMRespuestaInvalida):
            await cliente.generar_correo(DATOS)


async def test_json_validate_failed_de_groq_es_respuesta_invalida(settings):
    cuerpo = {"error": {"message": "Failed to validate JSON", "code": "json_validate_failed"}}
    async with cliente_con(settings, lambda _: httpx.Response(400, json=cuerpo)) as cliente:
        with pytest.raises(LLMRespuestaInvalida):
            await cliente.generar_correo(DATOS)


async def test_timeout_es_transitorio(settings):
    def manejador(peticion: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=peticion)

    async with cliente_con(settings, manejador) as cliente:
        with pytest.raises(LLMErrorTransitorio, match="timeout"):
            await cliente.generar_correo(DATOS)


async def test_error_de_red_es_transitorio(settings):
    def manejador(peticion: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin conexión", request=peticion)

    async with cliente_con(settings, manejador) as cliente:
        with pytest.raises(LLMErrorTransitorio, match="error de red"):
            await cliente.generar_correo(DATOS)


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_5xx_es_transitorio(settings, status):
    async with cliente_con(settings, lambda _: httpx.Response(status, text="caído")) as cliente:
        with pytest.raises(LLMErrorTransitorio, match=str(status)):
            await cliente.generar_correo(DATOS)


async def test_429_es_transitorio_con_retry_after_y_sin_datos_de_la_cuenta(settings):
    cuerpo = {"error": {"message": "Rate limit reached in organization `org_secreta`"}}
    respuesta = httpx.Response(429, json=cuerpo, headers={"retry-after": "7"})

    async with cliente_con(settings, lambda _: respuesta) as cliente:
        with pytest.raises(LLMErrorTransitorio) as error:
            await cliente.generar_correo(DATOS)

    assert error.value.reintentar_en == 7
    assert "org_secreta" not in str(error.value)


@pytest.mark.parametrize("status", [400, 401, 404])
async def test_4xx_es_permanente(settings, status):
    cuerpo = {"error": {"message": "Invalid API Key"}}
    async with cliente_con(settings, lambda _: httpx.Response(status, json=cuerpo)) as cliente:
        with pytest.raises(LLMErrorPermanente, match=f"{status}: Invalid API Key") as error:
            await cliente.generar_correo(DATOS)

    # La API key nunca aparece en el mensaje que termina en error_motivo.
    assert "clave-de-test" not in str(error.value)


# --- De punta a punta: worker + cliente real + Groq simulado + Postgres ---


async def procesar_con_groq_simulado(sesiones, settings, manejador) -> tuple[Lead, int]:
    llamadas = 0

    def contar(peticion: httpx.Request) -> httpx.Response:
        nonlocal llamadas
        llamadas += 1
        return manejador(peticion)

    async with sesiones() as session:
        lead = Lead(nombre_empresa="Acme", dominio="acme.com", cargo_contacto="CTO")
        session.add(lead)
        await session.commit()

    async with cliente_con(settings, contar) as cliente:
        await procesar_uno(sesiones, cliente, settings)

    async with sesiones() as session:
        return await session.get(Lead, lead.id), llamadas


async def test_worker_con_json_invalido_deja_el_lead_en_error(sesiones, settings):
    lead, llamadas = await procesar_con_groq_simulado(
        sesiones, settings, lambda _: respuesta_groq("no es json")
    )

    assert lead.estado == EstadoLead.ERROR
    assert lead.error_motivo.startswith("Respuesta inválida del LLM: JSON inválido")
    assert llamadas == 1


async def test_worker_con_timeout_reintenta_y_deja_el_lead_en_error(sesiones, settings):
    def manejador(peticion: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=peticion)

    lead, llamadas = await procesar_con_groq_simulado(sesiones, settings, manejador)

    assert lead.estado == EstadoLead.ERROR
    assert lead.error_motivo == (
        f"LLM no disponible tras 3 reintentos: timeout tras {settings.llm_timeout_seconds:g}s"
    )
    assert llamadas == settings.llm_max_retries + 1


async def test_worker_se_recupera_de_un_429(sesiones, settings):
    respuestas = iter(
        [
            httpx.Response(429, json={"error": {"message": "rate limit"}}),
            respuesta_groq(json.dumps({"asunto": "Hola", "cuerpo": "Correo breve."})),
        ]
    )

    lead, llamadas = await procesar_con_groq_simulado(
        sesiones, settings, lambda _: next(respuestas)
    )

    assert lead.estado == EstadoLead.PROCESADO
    assert lead.correo_asunto == "Hola"
    assert llamadas == 2
