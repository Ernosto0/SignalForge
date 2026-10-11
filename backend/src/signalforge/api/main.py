from fastapi import FastAPI

from signalforge import __version__
from signalforge.api.routes import agents, health, jobs, opportunities, plans, runs, sources

app = FastAPI(title="SignalForge", version=__version__)
for module in (health, agents, plans, runs, jobs, opportunities, sources):
    app.include_router(module.router, prefix="/api")
