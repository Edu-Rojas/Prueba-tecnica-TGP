# TGP Leads

Un microservicio que escribe el primer correo de prospección para leads B2B.

Le envías una empresa, su dominio y el cargo de la persona a contactar. El servicio guarda el
lead al instante y, en segundo plano, le pide a un modelo de lenguaje (Groq) un correo corto y
personalizado para esa empresa y ese cargo. Cada lead pasa por estos estados:

```
pendiente  →  procesando  →  procesado   (el correo quedó guardado)
                          ↘  error       (con el motivo explicado)
```

**Desplegado en:** https://tgp-leads.onrender.com

Documentación interactiva (Swagger): https://tgp-leads.onrender.com/docs. Desde ahí se pueden crear
y listar leads.

Plan gratuito de Render: si no recibe tráfico en ~15 min se duerme y el primer request tarda
cerca de un minuto.

## Cómo levantarlo en local

Requisitos: Docker. Para correr los tests, además [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
```

Abre `.env` y completa dos cosas:

- `GROQ_API_KEY`: tu clave de Groq (se crea gratis en https://console.groq.com/keys).
- `POSTGRES_PASSWORD`: cualquier contraseña. Pon la misma dentro de `DATABASE_URL`.

El `.env` está en `.gitignore`, así que el repositorio no tiene ninguna clave. Con el setup listo:

```bash
docker compose up -d --build --wait
```

Se arrancan tres contenedores:
- `db`: la base de datos PostgreSQL.
- `api`: crea las tablas y queda escuchando en http://localhost:8000.
- `worker`: el proceso que toma los leads pendientes y les genera el correo.

### Para probarlo

```bash
curl -X POST localhost:8000/leads -H "content-type: application/json" \
  -d '{"nombre_empresa":"Rappi","dominio":"rappi.com","cargo_contacto":"Head of Growth"}'
```

Este comando crea un lead: en este caso, Rappi, para contactar al Head of Growth. Lo importante es
que la respuesta llega enseguida, con el lead en estado `pendiente`. Unos segundos después:

```bash
curl localhost:8000/leads
```

Esto lista los leads; el de Rappi ya aparece `procesado`, con el asunto y el cuerpo del correo.

> **Si usas PowerShell en Windows:** ahí `curl` no es el curl real sino otro comando, y estos
> ejemplos dan error. Usa Git Bash, o simplemente la página http://localhost:8000/docs.

### Cambiar el remitente del correo

El correo lo firma quien esté configurado en `REMITENTE_NOMBRE` y `REMITENTE_EMPRESA`, y
`REMITENTE_PROPUESTA` dice qué se ofrece. Vienen con valores de ejemplo (Amelia Honorato, de
The Growth Pro). Para cambiarlos, edítalos en tu `.env` y reinicia:

```bash
docker compose up -d
```

Si no están en el `.env`, se usan los valores por defecto de `app/config.py` (líneas 29 a 34). Si
cambias ese archivo, hay que reconstruir la imagen con `docker compose up -d --build`. En Render
se configuran en la sección *Environment* del servicio.

### Para correr los tests

Usan su propia base de datos, `leads_test`, que se crea sola, así que no tocan tus datos.

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

La primera vez, `uv` crea el entorno virtual e instala las dependencias.

Todas las variables están explicadas en `.env.example`.

## Decisiones de diseño

**El worker.** Es un proceso aparte que revisa cada pocos segundos la tabla de leads. No usé
Celery ni Redis: para este volumen, la propia tabla funciona como cola y me ahorro un servicio
más que mantener.

El proceso de cada lead es el siguiente:

1. **Lo reclama.** En una transacción corta toma el lead pendiente más antiguo, lo marca como
   `procesando` y confirma. Para elegirlo usa `SELECT ... FOR UPDATE SKIP LOCKED`: si otro worker
   ya está tomando una fila, esta la salta en vez de esperarla. Así pueden correr varios workers a
   la vez sin que dos agarren el mismo lead.
2. **Llama al LLM fuera de cualquier transacción.** Una llamada tarda segundos; con la
   transacción abierta se ocuparía una conexión y la fila quedaría bloqueada todo ese tiempo.
3. **Guarda el resultado en otra transacción corta**: `procesado` con el correo, o `error` con el
   motivo. Solo lo guarda si el lead sigue a su cargo. Si por algún motivo otro worker lo retomó
   mientras tanto, no pisa su trabajo.

Si un worker muere a mitad de un lead, la recuperación lo devuelve a `pendiente` tras 5 minutos
en `procesando`, o lo deja en `error` si ya se intentó 3 veces.

En Render el plan gratuito no tiene workers aparte, así que con `RUN_WORKER_IN_API=true` el
mismo código corre como tarea async dentro del `lifespan` de la API. Por eso todo es async
(SQLAlchemy + asyncpg + httpx): mientras el worker espera al LLM, la API sigue respondiendo. En un
plan pago, el worker correría como servicio aparte y esto no haría falta.

Elegí Render por varias razones: el deploy queda descrito en código (`render.yaml` en el repo),
tiene healthcheck y se despliega fácil desde GitHub. Railway tenía pocos créditos gratuitos y no
quería arriesgarme a que dejara de funcionar, y Fly.io solo ofrecía una prueba corta. Para este
alcance Render funciona bien aunque no tenga un worker aparte gratis, y encontramos una buena
solución. Si hubiera que escalar, se pueden buscar alternativas pagas.

### Cuando el LLM falla

Depende del tipo de falla:

- **Fallas pasajeras** (la llamada tarda demasiado, se cae la red, Groq responde 429 o 5xx): se
  reintenta hasta 3 veces, esperando cada vez un poco más, y si Groq indica cuánto esperar, se
  respeta. Cada llamada tiene un límite de 20 segundos. Si los reintentos se agotan, el lead
  queda en `error` con el motivo "LLM no disponible tras 3 reintentos".
- **Fallas definitivas** (clave inválida, modelo inexistente): reintentar no sirve, así que el lead
  pasa a `error` de inmediato, con el mensaje de Groq.
- **Respuestas que no sirven:** todo lo que devuelve el modelo se revisa antes de guardarse. Se
  descarta si no tiene el formato esperado (`{"asunto", "cuerpo"}`), si es demasiado largo, si deja
  textos de relleno como `[Nombre]` o `Tu Empresa`, si se despide, o si no menciona a la empresa
  del lead o a la empresa que escribe. En ese caso se pide otro correo, hasta 3 veces en total. Un
  correo que no pasa la revisión nunca se guarda como `procesado`.

### El correo

El modelo recibe quién escribe y qué ofrece, y redacta el cuerpo con eso. La firma, en cambio, la
agrega el código: así se evita que el LLM deje textos de relleno como `[Tu Empresa]` al firmar.

### La base de datos

Una sola tabla, `leads`, con estas decisiones:

- `estado` es un enum: la base solo acepta los cuatro estados válidos (`pendiente`, `procesando`,
  `procesado`, `error`).
- **Un índice sobre `(estado, created_at)`**, que es justo la búsqueda del worker: el pendiente
  más antiguo. Con pocos datos Postgres no lo usa, porque le sale más barato leer la tabla entera
  que entrar al índice. El índice queda creado desde la migración de Alembic para cuando la tabla
  crezca. Lo medí con 50 000 filas: la consulta del worker tarda unos 0,09 ms con el índice y unos
  7 ms sin él, unas 70 a 80 veces más rápido.
- Reglas en la propia base: un lead `procesado` siempre tiene correo y uno en `error` siempre tiene
  motivo. Así, aunque el código tuviera un error, los datos no pueden quedar inconsistentes.
- Fechas con zona horaria, siempre en UTC.
- El motivo de error tiene un máximo de 255 caracteres y se recorta antes de guardarse. Sin eso,
  un mensaje de error muy largo haría fallar el guardado y el lead quedaría trabado.
- Las tablas se crean con migraciones de Alembic, que corren al arrancar la API. El worker nunca
  migra, para que no haya dos procesos haciéndolo a la vez.

**Secretos.** Todo va por variables de entorno. La API key es `SecretStr`: solo viaja en el header
y no aparece en logs ni en `error_motivo`, y aunque alguien imprima la configuración completa, la
clave sale oculta.

## Qué mejoraría con más tiempo

- Reintentar leads en error: hoy quedan ahí. Haría falta un endpoint para volver a procesarlos.
- Correos menos repetitivos: muchos cierran con la misma pregunta ("¿Tienes 15 minutos esta
  semana?"). Revisaría cómo se escribe un buen correo de prospección y ajustaría el prompt.
- Revisión más fina de los correos: uno de los de prueba terminaba en "??" en lugar de "?". Es una
  falla menor pero corregible.
- Evitar leads duplicados: si el cliente reenvía la misma petición, hoy se crea un lead nuevo.
- Más velocidad: cada worker procesa un lead a la vez. Podría procesar varios en paralelo,
  respetando el límite de uso de Groq.
- Avisar al worker en vez de consultar a cada rato: usar `LISTEN/NOTIFY` de Postgres para que la
  API avise cuando entra un lead nuevo, dejando la consulta periódica como respaldo.
- Pasar a una cola dedicada (Redis, RabbitMQ) si el volumen fuera mucho mayor.
- Métricas: cuántos leads hay en cada estado, cuánto tarda el modelo y con qué frecuencia falla.
- Sacar el remitente de una base de datos de la empresa que envía el correo, en vez de una
  variable de entorno.
- Una base de datos permanente: la gratuita de Render expira después de un tiempo.
