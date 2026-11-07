from fastapi import FastAPI
from gateway.db import init_db

app = FastAPI(title="TraceChain Gateway")

projects_db = []
@app.on_event("startup")
def startup():
    init_db()

@app.get("/health")
def health():
    return {"status": "gateway ok"}

@app.post("/donations")
def create_donation(donation: dict):
    # TODO Phase 2: forward this to validator node
    return {"status": "received", "donation": donation}

@app.get("/trace")
def get_trace():
    return {"trace": []}

@app.post("/projects")
def create_project(project: dict):
    projects_db.append(project)
    return {"status": "created", "project": project}

@app.get("/projects")
def list_projects():
    return {"projects": projects_db}

@app.post("/allocations")
def create_allocation(allocation: dict):
    return {
        "status": "allocation created",
        "memo": allocation.get("project_name"),
        "amount": allocation.get("amount")
    }