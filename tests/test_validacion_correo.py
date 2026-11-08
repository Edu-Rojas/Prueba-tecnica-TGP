"""Validaciones del correo que devuelve el LLM. Los casos rechazados salen de respuestas reales."""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.llm.base import CorreoGenerado, DatosLead, LLMRespuestaInvalida, validar_contenido
from app.llm.prompt import agregar_firma, construir_prompt_sistema

PREGUNTA = "¿Tienes 15 minutos esta semana?"


@pytest.mark.parametrize(
    ("cuerpo", "fragmento_error"),
    [
        # Basados en respuestas reales de Groq vistas al probar contra la API real.
        ("En [Tu Empresa] ayudamos a equipos de logística. " + PREGUNTA, "[Tu Empresa]"),
        ("Hola,\n\nTe escribo por Despegar. " + PREGUNTA + "\n\nSaludos,\n\n[Nombre]", "[Nombre]"),
        ("Hola, ayudamos a Bimbo con RRHH. " + PREGUNTA + "\n\nUn saludo,", "despedida"),
        # Variantes del mismo problema.
        ("En Tu Empresa ayudamos a equipos. " + PREGUNTA, "Tu Empresa"),
        ("Hola <Nombre>, " + PREGUNTA, "<Nombre>"),
        ("Hola, soy XXX. " + PREGUNTA, "XXX"),
        ("Hola, {empresa} te ayuda. " + PREGUNTA, "{empresa}"),
        ("Hola. " + PREGUNTA + "\nSaludos", "despedida"),
        ("Hola. " + PREGUNTA + "\nAtentamente.", "despedida"),
        ("palabra " * 121, "121 palabras"),
    ],
)
def test_rechaza_correos_que_no_se_pueden_enviar_tal_cual(cuerpo, fragmento_error):
    with pytest.raises(ValidationError, match=fragmento_error.replace("[", r"\[")):
        CorreoGenerado(asunto="Asunto", cuerpo=cuerpo)


@pytest.mark.parametrize(
    "cuerpo",
    [
        # "tu empresa" en minúsculas es texto normal, no un marcador.
        "Hola, ayudamos a tu empresa a decidir con datos. " + PREGUNTA,
        # Agradecer en medio del texto no es una despedida final.
        "Hola, gracias por leer. Te escribo por Acme. " + PREGUNTA,
    ],
)
def test_acepta_correos_validos(cuerpo):
    assert CorreoGenerado(asunto="Asunto", cuerpo=cuerpo).cuerpo == cuerpo


def test_rechaza_asunto_demasiado_largo():
    with pytest.raises(ValidationError):
        CorreoGenerado(asunto="a" * 121, cuerpo="Hola. " + PREGUNTA)


@pytest.mark.parametrize(
    ("empresa_lead", "texto"),
    [
        ("Acme", "En Nexo Analytics ayudamos a Acme."),
        ("ACME", "En Nexo Analytics ayudamos a acme."),
        # Nombre legal largo: alcanza con la palabra que identifica a la empresa.
        ("Grupo Bimbo S.A. de C.V.", "En Nexo Analytics ayudamos a Bimbo."),
    ],
)
def test_validar_contenido_acepta_si_nombra_al_lead_y_al_remitente(empresa_lead, texto):
    correo = CorreoGenerado(asunto="Hola", cuerpo=texto + " " + PREGUNTA)

    validar_contenido(correo, DatosLead(empresa_lead, "x.com", "CTO"), "Nexo Analytics")


def test_validar_contenido_no_confunde_palabras_parciales():
    # "Nu" no debe darse por mencionado solo porque aparece dentro de "Nueva".
    correo = CorreoGenerado(asunto="Hola", cuerpo="Nexo Analytics y una Nueva idea. " + PREGUNTA)

    with pytest.raises(LLMRespuestaInvalida, match="no menciona a Nubank"):
        validar_contenido(correo, DatosLead("Nubank", "nubank.com", "CTO"), "Nexo Analytics")


def test_la_firma_la_agrega_el_codigo_con_el_remitente_configurado():
    settings = Settings(remitente_nombre="Ana Paz", remitente_empresa="Datos SA")

    assert agregar_firma("Hola. " + PREGUNTA + "\n", settings) == (
        "Hola. " + PREGUNTA + "\n\nSaludos,\nAna Paz\nDatos SA"
    )


def test_el_prompt_incluye_empresa_y_propuesta_del_remitente():
    settings = Settings(remitente_empresa="Datos SA", remitente_propuesta="tableros de ventas")

    prompt = construir_prompt_sistema(settings)

    assert "SDR (representante de ventas B2B) de Datos SA" in prompt
    assert "Lo que ofrece Datos SA: tableros de ventas." in prompt
    # El ejemplo JSON queda con llaves simples tras el .format().
    assert '{"asunto": "<asunto del correo>", "cuerpo": "<cuerpo del correo>"}' in prompt
