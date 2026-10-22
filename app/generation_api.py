"""Generation and retrieval endpoints. Staleness is attached to every read."""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import browse
from app.generation import generate
from app.llm import make_llm
from app.schemas import GenerateIn
from app.staleness import assess
from app.store import TinyDBStore


def register(app: FastAPI) -> None:
    from app.main import get_db

    settings = app.state.settings
    app.state.store = TinyDBStore(settings.generation_store_path)
    app.state.llm = make_llm(settings)

    def view(db: Session, g: dict, include_attempts: bool = False) -> dict:
        out = {k: v for k, v in g.items() if k != "attempts" or include_attempts}
        out["attempt_count"] = len(g.get("attempts", []))
        out["staleness"] = assess(db, g) if g["status"] == "succeeded" else None
        return out

    @app.post("/selections/{selection_id}/generations", status_code=201,
              summary="Generate 3-5 QA test cases for a selection")
    def create_generation(selection_id: str, request: Request, body: GenerateIn | None = None,
                          db: Session = Depends(get_db)):
        sel = browse.get_selection(db, selection_id)
        g, reused = generate(sel, request.app.state.llm, request.app.state.store,
                             max_attempts=settings.llm_max_attempts, force=bool(body and body.force))
        payload = {"reused": reused, **view(db, g, include_attempts=g["status"] == "failed")}
        if g["status"] == "failed":
            return JSONResponse(status_code=502, content=payload)
        return JSONResponse(status_code=200 if reused else 201, content=payload)

    @app.get("/selections/{selection_id}/generations")
    def selection_generations(selection_id: str, request: Request, db: Session = Depends(get_db)):
        browse.get_selection(db, selection_id)
        return [view(db, g) for g in request.app.state.store.find(selection_id=selection_id)]

    @app.get("/generations", summary="Fetch generations by selection or by node (any version of it)")
    def list_generations(request: Request, selection_id: str | None = None, node_id: str | None = None,
                         status: str | None = Query(default=None, description="succeeded | failed"),
                         db: Session = Depends(get_db)):
        if not selection_id and not node_id:
            raise HTTPException(422, "give selection_id or node_id")
        logical_id = browse.get_node(db, node_id).logical_id if node_id else None
        rows = request.app.state.store.find(selection_id=selection_id, logical_id=logical_id, status=status)
        return [view(db, g) for g in rows]

    @app.get("/generations/{generation_id}")
    def get_generation(generation_id: str, request: Request, attempts: bool = False,
                       db: Session = Depends(get_db)):
        g = request.app.state.store.get(generation_id)
        if g is None:
            raise HTTPException(404, f"generation {generation_id!r} not found")
        return view(db, g, include_attempts=attempts)
