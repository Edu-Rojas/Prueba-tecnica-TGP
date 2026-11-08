# BRIEF

Construí el proyecto con Claude Code (Opus) en una sola sesión. Este documento resume cómo lo
dirigí, dónde se equivocó y cómo comprobé que funciona.

## 1. Mi brief

### Cómo partí el problema

Antes de pedir código busqué la información necesaria para cumplir la tarea, y con esa información
generé un prompt con lo que iba a utilizar. Para eso definí ciertas cosas:

1. **Contexto y requisitos:** qué hace el servicio (`POST /leads` con 202 sin llamar al LLM, un
   worker que genera el correo, `GET /leads`), qué se evalúa (que funcione de punta a punta, el
   manejo de fallas del LLM, tests de los cambios de estado, CI, commits chicos, cero secretos) y
   qué queda fuera. Hice de antemano un plan detallado de lo que iba a necesitar, así que terminé
   de definir los detalles y después tenía que ejecutar todo el plan y verificar que lo planeado
   estuviera bien hecho.
2. **Decisiones ya tomadas**: para que el agente no cambiara lo planeado, le puse como regla no
   cambiar las tecnologías: Python 3.12, FastAPI, SQLAlchemy 2.0 + Alembic, uv, tests contra
   Postgres real con el LLM mockeado, Groq detrás de un protocolo `LLMClient`. Le definí
   exactamente qué esperaba, qué era el proyecto y cómo íbamos a hacer cada parte, además de un
   paso a paso en 6 fases que teníamos que completar.
3. **Reglas de trabajo:**

   > 1. Trabaja POR FASES. Al terminar cada fase, DETENTE y dame: un resumen de lo que hiciste,
   >    cómo lo verifico (comandos exactos) y un mensaje de commit propuesto. No hagas commits tú.
   > 2. No agregues dependencias ni servicios que no estén arriba sin justificarlo y preguntarme.
   > 3. Ningún secreto en el código ni en los logs.
   > 4. Si algo de mis decisiones te parece incorrecto, dímelo con argumentos ANTES de implementarlo.
   > 5. Código simple y legible por encima de lo "inteligente". Comenta solo el porqué.

   La gran mayoría de esto es lo que pide textualmente la actividad, así que me estaba asegurando
   de que lo hiciera tal como se pide.

   En Obsidian fui tomando notas del proceso. Básicamente usé Gemini Pro (high effort) y Claude
   Opus 5.5 (high) para investigar las tecnologías, definir las decisiones y armar el prompt
   inicial. Edité todo lo que quería, combiné lo que me dijeron en un primer prompt guardado en un
   .txt y luego solo le dije que empezáramos la fase 0.

Las fases fueron: 0) plan y preguntas, sin código; 1) esqueleto y Docker; 2) modelo y migración;
3) endpoints; 4) cliente LLM y worker (en una rama aparte); 5) tests y CI; 6) modo
`RUN_WORKER_IN_API`, deploy y README. Trabajar por fases y revisar cada una antes de seguir es lo
que me permitió entender cada línea, que es lo que voy a necesitar en la entrevista.

### Prompts de seguimiento que cambiaron el resultado

- En la fase 0 el agente hizo preguntas (cliente de Groq, qué hacer cuando se agotan los
  reintentos, plataforma de deploy, validación del dominio) y propuso cambios a mi diseño. Acepté
  ya que eran mejoras de la idea inicial.
- En la fase 0 habíamos acordado SQLAlchemy síncrono con psycopg. Después de la fase 2 cambié de
  idea y le pedí que usara AsyncEngine y AsyncSession para que el worker no bloquee todo, las marcas
  de tiempo en UTC y String(255) en nombre_empresa, dominio, cargo_contacto y error_motivo. Antes de
  implementarlo, el agente me avisó que el modo async de psycopg no funciona en Windows, y elegí
  asyncpg.
- Me tuve que asegurar que no hubiera bugs con el maximo de caracteres en los errores, por si acaso pasa algo con 255
  caracteres, un mensaje largo habría hecho fallar el `UPDATE` y el lead habría quedado en
  `procesando`. Se resolvió en el tipo de la columna, para que aplique a cualquier escritura.
- le indique instrucciones mas claras de que teniamos que tener con certeza que ningun correo podria salir con un tag.
  Ya habia revisiones para evitarlo pero arreglamos un poco mas el prompt.

### El prompt inicial completo

Este es el brief con el que arranqué la sesión de Claude Code:

```text
# Contexto

Estoy haciendo una prueba técnica para un puesto de Backend Junior. Tengo aprox. 4 horas.
Necesito construir un microservicio que genere un primer correo de outreach B2B personalizado
para un lead usando un LLM, y que persista todo en PostgreSQL.

Lo que evalúan: que funcione de extremo a extremo (docker compose up + URL pública),
el manejo de fallas del LLM, tests que prueben los cambios de estado, CI en verde,
commits chicos, una rama con PR, un esquema de BD razonable y cero secretos en código, logs o historial.
Después tendré una entrevista técnica SIN IA, así que tengo que entender cada línea.

## Requisitos funcionales

- POST /leads: recibe {"nombre_empresa", "dominio", "cargo_contacto"}, lo guarda con estado
  "pendiente" y responde de inmediato (202) SIN llamar al LLM.
- Un worker en segundo plano toma los leads pendientes, genera un correo corto y personalizado
  y deja el lead en "procesado", o en "error" guardando un motivo claro.
- GET /leads: lista los leads con estado, correo generado (si existe) y fecha de creación.
- Fuera de alcance: frontend, autenticación, enriquecimiento externo.

## Decisiones ya tomadas (no las cambies sin consultarme)

- Python 3.12, FastAPI, Pydantic v2, pydantic-settings para la config.
- PostgreSQL con SQLAlchemy 2.0 y migraciones con Alembic.
- Gestor de dependencias: uv.
- Worker: un proceso aparte que hace polling a la tabla `leads` usando
  `SELECT ... FOR UPDATE SKIP LOCKED`. Nada de Celery/Redis y nada de BackgroundTasks de FastAPI.
  - IMPORTANTE: el lock NO se mantiene durante la llamada al LLM. El flujo es:
    (1) transacción corta: reclamar un lead y marcarlo "procesando" + commit;
    (2) llamar al LLM fuera de cualquier transacción;
    (3) transacción corta: guardar el resultado como "procesado" o "error".
  - Recuperación: los leads que lleven más de N minutos en "procesando" vuelven a "pendiente".
  - El mismo código del worker tiene que poder correr de dos formas: como proceso independiente
    (local, servicio aparte en compose) o embebido en la API como tarea async en el `lifespan`,
    activado con la variable RUN_WORKER_IN_API=true (para el deploy en free tier).
- LLM: Groq (free tier) detrás de una interfaz/protocolo `LLMClient`, para poder inyectar
  un cliente falso en los tests.
  - Se le pide al modelo que responda en JSON {"asunto": str, "cuerpo": str} y se valida con Pydantic.
  - Timeout explícito en cada llamada.
  - Reintentos con backoff (tenacity) SOLO para errores transitorios (timeout, 429, 5xx).
  - Una respuesta inválida o un error permanente deja el lead en "error" con un motivo legible.
  - Columnas `intentos` y `error_motivo`.
- Tabla `leads`: id (UUID), nombre_empresa, dominio, cargo_contacto, estado (enum),
  correo_asunto, correo_cuerpo, error_motivo, intentos, created_at, updated_at.
  Índice sobre (estado, created_at).
- Tests: pytest contra un Postgres real (no SQLite) y con el LLM mockeado.
- CI: GitHub Actions con Postgres como service container.
- Local: docker compose con los servicios db, api y worker (misma imagen, distinto comando),
  con healthcheck en la db.

## Reglas de trabajo

1. Trabaja POR FASES. Al terminar cada fase, DETENTE y dame:
   un resumen de lo que hiciste, cómo lo verifico (comandos exactos)
   y un mensaje de commit propuesto (Conventional Commits). No hagas commits tú.
2. No agregues dependencias ni servicios que no estén arriba sin justificarlo y preguntarme.
3. Ningún secreto en el código ni en los logs: todo va por variables de entorno, con un `.env.example`.
   Nunca loguees la API key ni headers de autorización.
4. Si algo de mis decisiones te parece incorrecto, dímelo con argumentos ANTES de implementarlo.
5. Código simple y legible por encima de lo "inteligente". Comenta solo el porqué, no el qué.

## Fases

- Fase 0: plan. Propón la estructura de carpetas, confirma las fases y hazme las preguntas que tengas.
  NO escribas código todavía.
- Fase 1: esqueleto (pyproject, config, Dockerfile, docker-compose, .env.example, .gitignore, GET /health).
- Fase 2: modelo SQLAlchemy y migración inicial con Alembic.
- Fase 3: POST /leads y GET /leads con sus schemas.
- Fase 4 (irá en una rama aparte): cliente LLM, worker, transiciones de estado y manejo de errores.
- Fase 5: tests (pendiente→procesado, pendiente→error cuando el LLM falla, JSON inválido, timeout,
  POST no llama al LLM) y el workflow de CI.
- Fase 6: modo RUN_WORKER_IN_API para el deploy y README (cómo levantar el proyecto, decisiones, mejoras).
```

## 2. Cuando la IA se equivocó

### Correos con `[Tu Empresa]` en producción (el más importante)

Al probar la URL de Render, uno de los dos leads quedó en `error`:

```
error_motivo = 'Respuesta inválida del LLM: JSON inválido del LLM (cuerpo: Value error,
                contiene un marcador sin reemplazar: [Tu Empresa])'
```

La validación hizo su trabajo, pero ese lead se perdió.

La primera propuesta del agente no sirvió. Propuso agregar al prompt una regla para que no
firmara. La medimos con 12 llamadas reales con el prompt de ese momento y 12 con el corregido.
El resultado fue 1 de 12 inválidos con el prompt original y 2 de 12 con la supuesta corrección.
No se aplicó.

Para encontrar la causa pedí ver los correos completos, no solo el resultado de la
validación. Al leer 8 correos reales se vio el problema: el modelo no sabía quién
escribía ni qué se vendía. Inventaba un producto distinto en cada correo y, al querer 
nombrar su empresa o firmar, ponía `[Tu Empresa]` o `[Nombre]`. Además encontré cosas 
que la validación de ese momento dejaba pasar: correos que terminaban en "Un saludo," 
sin firma, mezcla de "tú", "ustedes" y "vuestra", y prueba social inventada ("otras instituciones del sector").

para corregirlo el agente propuso variables REMITENTE_* en el prompt. pero lo cambiamos a una version hibrida
 entre las soluciones que me habia dejado: el modelo escribe la empresa y la propuesta en el cuerpo (para que el correo diga qué se vende),
pero la firma la agrega el código, basicamente, si la firma es algo que siempre va, para que dejarselo a la LLM, se cambia en 2 lineas 
se me ocurrio la idea de llamarlo con una base de datos de remitentes para que cualquier persona de la empresa lo pueda usar pero eso es
una idea aparte

Además:

- Validaciones nuevas: despedida al final, marcadores sin corchetes (`Tu Empresa`, `<Nombre>`,
  `XXX`), asunto de más de 120 caracteres, y que el correo mencione al lead y a la empresa
  remitente.
- Si la respuesta es inválida, se pide otro correo, hasta 3 generaciones, antes de pasar a
  `error`. Esto es muy importante para no perder leads por una mala generación: los inválidos
  aparecían en más o menos 1 de cada 12 generaciones, así que pedir otro correo casi siempre
  resuelve el problema.

**Resultado** con 12 leads reales por el pipeline completo: 12 procesados, 0 descartados, todos
con la firma correcta. Que nunca salga un correo roto no depende de esa muestra, que es chica:
está garantizado por la validación y cubierto por tests.
Así se ve un correo real, generado por el pipeline completo con el remitente actual
(Amelia Honorato, de The Growth Pro):

```
1. Mercado Libre · Gerente de Logística · procesado
ASUNTO: Optimizar logística con datos unificados

Hola, en Mercado Libre la eficiencia logística depende de la velocidad para tomar decisiones.
The Growth Pro conecta tus datos de ventas, marketing y operaciones en tableros automáticos,
eliminando la dependencia de TI y permitiendo a tu equipo de logística reaccionar en tiempo real.
¿Tienes 15 minutos esta semana para ver cómo funciona?

Saludos,
Amelia Honorato
The Growth Pro
```

Las pruebas de la sección 3 se hicieron antes de cambiar el remitente, por eso esos correos
están firmados por "Laura Méndez, Nexo Analytics", el valor de ejemplo anterior.

### El modelo por defecto no existía

Cuando hice las primeras investigaciones ambos modelos me indicaron que llama era una buena opcion
asi,se configuró `llama-3.3-70b-versatile` como modelo por defecto sin verificarlo. En la
primera prueba real el lead quedó en error porque `Groq respondió 404: The model 'llama-3.3-70b-versatile' does not exist`.
Lo bueno: el manejo de errores funcionó (error permanente, sin reintentos, motivo legible). Listamos los modelos
disponibles con la API de Groq, probamos los candidatos con nuestro cliente (los `gpt-oss` fallaron con el modo JSON) y elegí `qwen/qwen3.8-27b`.
fue un poco prueba y error, pero el resultado quedo bien y funciona correctamente que es lo que importa.

### La CI dio verde construyendo una imagen rota

Al subir la rama del worker, la CI falló porque `uv.lock` no estaba en el commit. Pero el job de
Docker **pasó**: el Dockerfile usaba `uv sync --frozen`, que instala lo que dice el lock sin
compararlo con `pyproject.toml`. La imagen se construyó sin `httpx` ni `tenacity` y el worker
habría fallado al arrancar. Lo vi al leer el log de la CI. Se cambió a `--locked`, y comprobé que
con el lock viejo el build ahora falla.

### Otras cosas que no me convencieron

- **Contenedor que no apagaba limpio.** En la simulación del deploy, `docker stop` tardaba 10 s:
  con `CMD ["sh", "-c", ...]`, el PID 1 era `sh` y no reenviaba el SIGTERM a uvicorn. Se agregó
  `exec`; ahora tarda 1 s y el log muestra "Worker detenido".
- **El README era demasiado largo.** El primero tenía tablas de variables, de fallas y de API.
  lo arregle a mano pero primero le indique que lo hiciera mas corto: cómo levantarlo, decisiones y mejoras.

## 3. Cómo comprobé que funciona

### Tests (81, contra Postgres real)

```bash
uv run pytest
# 81 passed
```

Corren contra una BD Postgres aparte (`leads_test`) con las migraciones reales de Alembic. El LLM
siempre es falso: un `FakeLLM` o el cliente real de Groq contra un servidor simulado
(`httpx.MockTransport`). Cubren:

- `POST /leads` guarda `pendiente`, responde 202 y **no llama al LLM** (el test reemplaza
  `generar_correo` por una función que falla si se la llama).
- pendiente → procesado; pendiente → error por error permanente, JSON inválido, timeout (4
  llamadas: 1 + 3 reintentos) y respuesta inválida persistente (3 generaciones).
- Reintento tras un 429 respetando `Retry-After`; el ID de la cuenta de Groq no aparece en el
  motivo.
- Dos workers no toman el mismo lead (`SKIP LOCKED`); el guardado condicional no pisa a otro
  worker; recuperación de atascados.
- Validación de correos con casos sacados de respuestas reales de Groq.
- Modo embebido: la API responde en menos de 0,3 s mientras el LLM tarda 0,5 s.

para saber si los tests funcionan rompimos el código a propósito y los corrí:

| Cambio introducido | Resultado |
|---|---|
| Quitar `AND intentos = ...` del guardado condicional | Falla `test_no_pisa_el_resultado_si_otro_worker_retomo_el_lead` |
| Reintentar cualquier error del LLM, no solo los transitorios | Fallan 3 tests de errores permanentes |

Al restaurar el código, todos pasaron. La CI corre lint, tests con Postgres como service container
y el build de Docker en cada PR.

### Contra la URL pública (Render)

Script de verificación con `httpx` sobre https://tgp-leads.onrender.com:

```
[OK] GET /health -> {"status":"ok"} [HTTP 200]
[OK] POST Mercado Libre -> 202 pendiente dominio='mercadolibre.com' en 271ms
     (se envió "https://www.MercadoLibre.com/ayuda": el dominio se normaliza)
[OK] POST Rappi -> 202 pendiente dominio='rappi.com' en 208ms
[OK] POST dominio inválido -> 422
[OK] POST empresa vacía -> 422
[OK] POST 256 caracteres -> 422
[OK] POST falta un campo -> 422
[OK] los inválidos no se guardaron
[OK] GET limit=1 / GET limit=101 -> 422
[OK] worker procesó Rappi -> procesado, intentos=1, 71 palabras
[FALLA] worker procesó Mercado Libre -> error: marcador sin reemplazar: [Tu Empresa]
```

Esa falla es la que se describe en la sección 2.

Después de desplegar el fix (PR #3), la misma prueba contra la URL pública:

```
[OK] POST Mercado Libre -> 202 pendiente dominio='mercadolibre.com'
[OK] POST Rappi -> 202 pendiente dominio='rappi.com'
[OK] los 4 POST inválidos -> 422, no se guardaron
[OK] GET limit=1 / GET limit=101 -> 422
[OK] worker procesó Mercado Libre -> procesado en <= 2s, intentos=1, 72 palabras
[OK] worker procesó Rappi -> procesado en <= 2s, intentos=1, 70 palabras
TODO OK
```

Para comprobar que Render corría la versión nueva y no la anterior, revisé la firma, que solo
agrega el código nuevo: los dos correos terminan en `"\n\nSaludos,\nLaura Méndez\nNexo Analytics"`
y cuando funcionaba lo cambie por nombres de TGP.

Después mandé 8 leads más de una vez (Despegar, Globant, Clínica Alemana,
Grupo Bimbo S.A. de C.V., Falabella, Nubank, Cemex, Kavak):

```
Terminados en 10s: {'procesado': 8}
  procesado  intentos=1  69 palabras  firma=True  Despegar: Datos de producto en Despegar
  procesado  intentos=1  70 palabras  firma=True  Globant: Datos en tiempo real para tu equipo técnico
  procesado  intentos=1  64 palabras  firma=True  Clínica Alemana: Optimización de compras en Clínica Alemana
  procesado  intentos=1  82 palabras  firma=True  Grupo Bimbo S.A. de C.V.: Optimización de datos en Grupo Bimbo
  procesado  intentos=1  64 palabras  firma=True  Falabella: Conecta datos de marketing y ventas en Falabella
  procesado  intentos=1  75 palabras  firma=True  Nubank: Conectar datos sin depender de TI
  procesado  intentos=1  59 palabras  firma=True  Cemex: Optimización operativa en Cemex
  procesado  intentos=1  60 palabras  firma=True  Kavak: Datos de ventas en tiempo real para Kavak
```

En la base de Render quedan 11 leads `procesado` y 1 en `error`: el de Mercado Libre de la
primera prueba, anterior al fix.

Equivalente con curl:

```bash
curl https://tgp-leads.onrender.com/health
curl -X POST https://tgp-leads.onrender.com/leads -H "content-type: application/json" \
  -d '{"nombre_empresa":"Rappi","dominio":"rappi.com","cargo_contacto":"Head of Growth"}'
curl https://tgp-leads.onrender.com/leads
```

### En local, con el fix (docker compose)

Misma prueba contra `localhost:8000`, todo OK. Ejemplo de correo guardado:

```
Hola,

Como Head of Growth en Rappi, tu prioridad es escalar con precisión. En Nexo Analytics
conectamos tus datos de ventas, marketing y operaciones en tableros automáticos. Esto te permite
tomar decisiones rápidas basadas en datos reales, sin depender del equipo de TI para cada reporte.

¿Tendrías 15 minutos esta semana para ver cómo funciona?

Saludos,
Laura Méndez
Nexo Analytics
```

### Queries a la base

Las pruebas que modifican datos las corrí en una BD aparte (`leads_demo`), con la imagen real de
la API y del worker.

**Error permanente real** (worker con `GROQ_MODEL=modelo-que-no-existe`):

```sql
SELECT nombre_empresa, estado, intentos, error_motivo FROM leads;
```
```
 nombre_empresa | estado | intentos | error_motivo
 Acme           | error  |        1 | Error permanente del LLM: Groq respondió 404: The model
                                     `modelo-que-no-existe` does not exist or you do not have access to it.
```

En el log del worker hay **un solo** request a Groq: no reintentó un error permanente.

**Recuperación de leads atascados** (simulo un worker que murió dejando dos leads en
`procesando` hace 10 minutos, y después arranco un worker):

```sql
INSERT INTO leads (id, nombre_empresa, dominio, cargo_contacto, estado, intentos, updated_at) VALUES
 (gen_random_uuid(), 'Kavak', 'kavak.com', 'Gerente de Ventas', 'procesando', 1, now() - interval '10 minutes'),
 (gen_random_uuid(), 'Cemex', 'cemex.com', 'Director de Operaciones', 'procesando', 3, now() - interval '10 minutes');
-- arranco el worker y espero unos segundos
SELECT nombre_empresa, estado, intentos, error_motivo FROM leads ORDER BY nombre_empresa;
```
```
 Cemex | error     | 3 | Se interrumpió el procesamiento 3 veces (máximo de intentos alcanzado)
 Kavak | procesado | 2 |
```

Log: `Recuperación: 1 leads devueltos a pendiente, 1 pasados a error`.

**SKIP LOCKED** (la sesión A bloquea el pendiente más antiguo durante 4 s; la sesión B hace la
misma consulta en ese momento):

```sql
SELECT nombre_empresa FROM leads WHERE estado='pendiente' ORDER BY created_at LIMIT 1
FOR UPDATE SKIP LOCKED;
```
```
sesión A: Lead1
sesión B con SKIP LOCKED: Lead2
sesión B con FOR UPDATE NOWAIT: ERROR: could not obtain lock on row in relation "leads"
```

Un segundo worker toma el siguiente lead en vez de esperar o repetir el mismo.
