from app.llm.base import CorreoGenerado, DatosLead

CORREO_OK = CorreoGenerado(asunto="Hola Acme", cuerpo="Un correo breve y personalizado.")


class FakeLLM:
    """Cliente LLM falso: devuelve (o lanza) las respuestas en orden y registra las llamadas.

    Si se acaban las respuestas, repite la última (útil para "falla siempre").
    """

    def __init__(self, *respuestas: CorreoGenerado | Exception) -> None:
        self._respuestas = list(respuestas)
        self.llamadas: list[DatosLead] = []

    # Se usa como "async with", igual que GroqClient (lo necesita el lifespan de la API).
    async def __aenter__(self) -> "FakeLLM":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        pass

    async def generar_correo(self, lead: DatosLead) -> CorreoGenerado:
        self.llamadas.append(lead)
        indice = min(len(self.llamadas), len(self._respuestas)) - 1
        respuesta = self._respuestas[indice]
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta
