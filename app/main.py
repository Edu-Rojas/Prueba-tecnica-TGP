from fastapi import FastAPI

from app.config import get_settings
from app.logs import configurar_logging
from app.routes.leads import router as leads_router

settings = get_settings()
configurar_logging(settings.log_level)

app = FastAPI(title="TGP Leads", version="0.1.0")
app.include_router(leads_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
