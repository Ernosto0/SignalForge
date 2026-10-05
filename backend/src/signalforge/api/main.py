from fastapi import FastAPI

from signalforge import __version__
from signalforge.api.routes import health

app = FastAPI(title="SignalForge", version=__version__)
app.include_router(health.router, prefix="/api")
