# TGP Leads

Microservicio que recibe leads B2B (`POST /leads`), los guarda en PostgreSQL y, en segundo plano,
genera con un LLM (Groq) un primer correo de outreach personalizado. `GET /leads` lista los leads
con su estado (`pendiente`, `procesando`, `procesado`, `error`) y el correo generado.

**Desplegado en:** https://tgp-leads.onrender.com
Documentación interactiva (Swagger): https://tgp-leads.onrender.com/docs
Plan gratuito de Render: si no recibe tráfico en ~15 min se duerme y el primer request tarda
cerca de un minuto.

## Cómo levantarlo en local

Requisitos: Docker. Para correr los tests, además [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
docker compose up -d --build --wait
```

Antes del `docker compose up`, completa en `.env` `POSTGRES_PASSWORD` (también dentro de
`DATABASE_URL`) y tu `GROQ_API_KEY`. El repo no trae ningún secreto.

Levanta `db` (Postgres), `api` (aplica las migraciones y queda en http://localhost:8000) y
`worker` (misma imagen, otro comando).

```bash
curl -X POST localhost:8000/leads -H "content-type: application/json" \
  -d '{"nombre_empresa":"Rappi","dominio":"rappi.com","cargo_contacto":"Head of Growth"}'
curl localhost:8000/leads
```

Ojo en PowerShell: ahí `curl` es un alias de `Invoke-WebRequest` y estos comandos dan error.
Usa Git Bash, `Invoke-RestMethod` o la página `/docs`.

Tests (usan una BD aparte, `leads_test`, que se crea sola) y lint:

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Variables de entorno en `.env.example`. Las que cambian el correo: `REMITENTE_NOMBRE`,
`REMITENTE_EMPRESA` y `REMITENTE_PROPUESTA` (vienen con valores de ejemplo).

## Decisiones de diseño

**El worker.** Es un proceso aparte que hace polling a la tabla `leads`, sin Celery ni Redis: la
tabla ya es la cola. Cada lead pasa por tres pasos:

1. Transacción corta: toma el pendiente más antiguo con `SELECT ... FOR UPDATE SKIP LOCKED`, lo
   marca `procesando`, suma 1 a `intentos` y hace commit. `SKIP LOCKED` permite correr varios
   workers sin que dos tomen el mismo lead.
2. Llama al LLM **fuera de cualquier transacción**. Una llamada tarda segundos; con la
   transacción abierta se ocuparía una conexión y la fila quedaría bloqueada todo ese tiempo.
3. Transacción corta: guarda `procesado` o `error` con un `UPDATE ... WHERE estado='procesando'
   AND intentos=<el del paso 1>`. Si mientras tanto otro worker retomó el lead, no se pisa su
   resultado.

Si un worker muere a mitad de un lead, la recuperación lo devuelve a `pendiente` tras 5 minutos
en `procesando`, o lo deja en `error` si ya se intentó 3 veces.

En Render el plan gratuito no tiene workers aparte, así que con `RUN_WORKER_IN_API=true` el
mismo código corre como tarea async dentro del `lifespan` de la API. Por eso todo es async
(SQLAlchemy + asyncpg + httpx): mientras el worker espera al LLM, la API sigue respondiendo.

**Fallas del LLM.** El cliente de Groq (httpx directo, timeout en cada llamada) solo clasifica
la falla; los reintentos viven en el worker:

- Timeout, error de red, 429 y 5xx: hasta 3 reintentos con backoff exponencial, respetando
  `Retry-After`. Si se agotan: `error` con motivo "LLM no disponible tras 3 reintentos".
- 4xx (key inválida, modelo inexistente): `error` sin reintentar.
- Respuesta inválida: se pide otro correo, hasta 3 generaciones en total, y si ninguna sirve
  queda en `error`. Es inválida si no es el JSON `{"asunto", "cuerpo"}`, si tiene marcadores
  sin reemplazar (`[Nombre]`, `Tu Empresa`), si se despide, si supera el largo, o si no menciona
  al lead o a la empresa remitente. Un correo inválido nunca se guarda como `procesado`.

**El correo.** El modelo escribe el cuerpo con la empresa y la propuesta del remitente, que
recibe en el prompt. La firma la agrega el código: al principio el modelo inventaba
`[Tu Empresa]` o `[Nombre]` al firmar, y lo que puede ser determinista no se le deja al LLM.

**Base de datos.** Una tabla `leads` con `estado` como enum, índice `(estado, created_at)` para
la consulta del worker y el listado, y CHECK constraints: un lead `procesado` tiene correo y uno
en `error` tiene motivo. Fechas `timestamptz` en UTC. `error_motivo` es `VARCHAR(255)` con un
tipo que recorta antes de escribir, para que un mensaje largo no haga fallar el `UPDATE` y deje
el lead colgado. Migraciones con Alembic, que corren al arrancar la API (nunca en el worker, para
que no haya dos procesos migrando a la vez).

**Secretos.** Todo va por variables de entorno. La API key es `SecretStr`, solo viaja en el header
y no aparece en logs ni en `error_motivo`.

## Qué mejoraría con más tiempo

- Un endpoint para reintentar leads en `error`; hoy quedan ahí.
- Mejorar el prompt: revisar cómo debe ser un buen correo de prospección y ajustarlo a eso. Hoy
  muchos correos cierran con la misma pregunta.
- Idempotencia en `POST /leads`, para no duplicar un lead si el cliente reintenta la petición.
- Procesar varios leads en paralelo por worker, con un límite acorde al rate limit de Groq.
- Métricas (leads por estado, latencia y tasa de error del LLM) y logs en JSON.
- Pasar la base a un plan pago: la gratuita de Render expira.