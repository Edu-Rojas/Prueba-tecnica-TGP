import json
import logging

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.llm.base import (
    CorreoGenerado,
    DatosLead,
    LLMErrorPermanente,
    LLMErrorTransitorio,
    LLMRespuestaInvalida,
)
from app.llm.prompt import construir_mensaje_usuario, construir_prompt_sistema

logger = logging.getLogger(__name__)


class GroqClient:
    """Cliente de Groq vía su API compatible con OpenAI, usando httpx directo.

    No reintenta: solo clasifica cada falla como transitoria o permanente.
    La política de reintentos vive en el worker, así se puede probar con un cliente falso.
    """

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._model = settings.groq_model
        self._timeout = settings.llm_timeout_seconds
        self._prompt_sistema = construir_prompt_sistema(settings)
        # La API key solo vive en este header; nunca se loguea ni se incluye en mensajes de error.
        self._http = httpx.AsyncClient(
            base_url=settings.groq_base_url,
            headers={"Authorization": f"Bearer {settings.groq_api_key.get_secret_value()}"},
            # Solo los tests pasan un transport (httpx.MockTransport) para simular a Groq sin red.
            transport=transport,
        )

    async def __aenter__(self) -> "GroqClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._http.aclose()

    async def generar_correo(self, lead: DatosLead) -> CorreoGenerado:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._prompt_sistema},
                {"role": "user", "content": construir_mensaje_usuario(lead)},
            ],
            # Modo JSON de Groq: obliga al modelo a devolver un objeto JSON válido.
            "response_format": {"type": "json_object"},
            "temperature": 0.7,
            "max_tokens": 500,
        }

        try:
            respuesta = await self._http.post(
                "/chat/completions", json=payload, timeout=self._timeout
            )
        except httpx.TimeoutException as exc:
            raise LLMErrorTransitorio(f"timeout tras {self._timeout:g}s") from exc
        except httpx.TransportError as exc:
            raise LLMErrorTransitorio(f"error de red: {type(exc).__name__}") from exc

        self._verificar_status(respuesta)
        return self._parsear_correo(respuesta)

    @staticmethod
    def _verificar_status(respuesta: httpx.Response) -> None:
        codigo = respuesta.status_code
        if codigo < 400:
            return
        if codigo == 429:
            # Mensaje propio: el de Groq incluye el ID de la organización, que no debe
            # terminar en la BD ni en la API.
            raise LLMErrorTransitorio(
                "Groq respondió 429: límite de uso alcanzado",
                reintentar_en=GroqClient._segundos_retry_after(respuesta),
            )
        detalle = GroqClient._mensaje_de_error(respuesta)
        if codigo >= 500:
            raise LLMErrorTransitorio(f"Groq respondió {codigo}: {detalle}")
        # Groq devuelve 400 json_validate_failed cuando el modelo no logró generar JSON válido:
        # no es un error de nuestra petición sino una respuesta inválida del LLM.
        if "json_validate_failed" in respuesta.text:
            raise LLMRespuestaInvalida(f"el modelo no generó un JSON válido (Groq {codigo})")
        raise LLMErrorPermanente(f"Groq respondió {codigo}: {detalle}")

    @staticmethod
    def _segundos_retry_after(respuesta: httpx.Response) -> float | None:
        try:
            return float(respuesta.headers["retry-after"])
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _mensaje_de_error(respuesta: httpx.Response) -> str:
        # Solo el cuerpo de la respuesta (el mensaje de Groq), nunca los headers de la petición.
        try:
            return str(respuesta.json()["error"]["message"])[:200]
        except (ValueError, KeyError, TypeError):
            return respuesta.text[:200].strip()

    @staticmethod
    def _parsear_correo(respuesta: httpx.Response) -> CorreoGenerado:
        try:
            contenido = respuesta.json()["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMRespuestaInvalida("la respuesta de Groq no tiene el formato esperado") from exc

        try:
            return CorreoGenerado.model_validate_json(contenido)
        except ValidationError as exc:
            # Resumen legible de qué falló (campo y motivo), sin volcar todo el contenido.
            errores = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'json'}: {e['msg']}" for e in exc.errors()
            )
            raise LLMRespuestaInvalida(f"JSON inválido del LLM ({errores})") from exc
