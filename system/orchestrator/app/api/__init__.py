"""HTTP API routers for the orchestrator.

Each module here exposes one APIRouter that gets included into the main
FastAPI app in main.py. Splitting by domain keeps each file under ~150
lines and makes it obvious which endpoints write to which tables.
"""
