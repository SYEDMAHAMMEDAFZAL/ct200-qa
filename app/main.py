"""FastAPI application: ingestion, browse, selections, generation and retrieval."""
from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import browse
from app.config import ROOT, Settings, get_settings
from app.db import make_engine, make_sessionmaker
from app.ingest import change_report, ingest_pdf
from app.models import Document
from app.schemas import IngestOut, NodeDetail, NodeSummary, SearchHit, SelectionIn, SelectionOut, VersionOut


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="CT-200 QA traceability API", version="1.0.0")
    engine = make_engine(settings.database_url)
    app.state.settings = settings
    app.state.sessionmaker = make_sessionmaker(engine)
    _register(app)
    from app.generation_api import register as register_generation
    register_generation(app)
    return app


def get_db(request: Request):
    db: Session = request.app.state.sessionmaker()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _version_out(v) -> dict:
    return {"version_number": v.version_number, "source_filename": v.source_filename,
            "file_sha256": v.file_sha256, "title": v.title, "page_count": v.page_count,
            "created_at": v.created_at, "parse_warnings": v.parse_warnings, "change_summary": v.change_summary}


@contextmanager
def _local_pdf(upload: UploadFile | None, path: str | None):
    if upload is not None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / Path(upload.filename or "upload.pdf").name
            with dest.open("wb") as fh:
                shutil.copyfileobj(upload.file, fh)
            yield dest, upload.filename
        return
    if not path:
        raise HTTPException(422, "send a PDF as multipart field 'file' or give ?path=")
    p = (ROOT / path).resolve() if not Path(path).is_absolute() else Path(path)
    if not p.is_file():
        raise HTTPException(404, f"file {path!r} not found")
    yield p, p.name


def _register(app: FastAPI) -> None:
    @app.get("/health")
    def health():
        return {"status": "ok", "llm_provider": app.state.settings.llm_provider}

    @app.post("/documents/{slug}/versions", response_model=IngestOut, status_code=201,
              summary="Ingest a PDF as the next version of a document")
    def ingest(slug: str, file: UploadFile | None = File(default=None),
               path: str | None = Query(default=None, description="server-side path, e.g. data/ct200_manual_v2.pdf"),
               db: Session = Depends(get_db)):
        with _local_pdf(file, path) as (local, name):
            res = ingest_pdf(db, local, slug=slug, source_name=name)
        return {"created": res.created, "version": _version_out(res.version)}

    @app.get("/documents")
    def list_documents(db: Session = Depends(get_db)):
        docs = db.scalars(select(Document).order_by(Document.slug))
        return [{"slug": d.slug, "title": d.title, "versions": [v.version_number for v in d.versions]} for d in docs]

    @app.get("/documents/{slug}/versions", response_model=list[VersionOut])
    def list_versions(slug: str, db: Session = Depends(get_db)):
        return [_version_out(v) for v in browse.get_document(db, slug).versions]

    @app.get("/documents/{slug}/sections", response_model=list[NodeSummary],
             summary="Top-level sections (default: latest version)")
    def sections(slug: str, version: str = "latest", db: Session = Depends(get_db)):
        doc = browse.get_document(db, slug)
        return browse.top_level(db, browse.resolve_version(db, doc, version))

    @app.get("/documents/{slug}/tree", summary="Whole tree for one version")
    def full_tree(slug: str, version: str = "latest", db: Session = Depends(get_db)):
        doc = browse.get_document(db, slug)
        v = browse.resolve_version(db, doc, version)
        return {"document": slug, "version": v.version_number, "title": v.title, "tree": browse.tree(db, v)}

    @app.get("/nodes/{node_id}", response_model=NodeDetail)
    def get_node(node_id: str, db: Session = Depends(get_db)):
        return browse.node_detail(db, browse.get_node(db, node_id))

    @app.get("/nodes/{node_id}/changes", summary="Has this section changed across versions? With diff summary")
    def node_changes(node_id: str, db: Session = Depends(get_db)):
        return change_report(db, browse.get_node(db, node_id))

    @app.get("/search", response_model=list[SearchHit])
    def search(q: str = Query(min_length=2), document: str = "ct200",
               version: str = Query(default="latest", description="a number, 'latest', or 'all'"),
               field: Literal["heading", "text", "both"] = "both", limit: int = Query(default=20, le=100),
               db: Session = Depends(get_db)):
        doc = browse.get_document(db, document)
        v = None if version == "all" else browse.resolve_version(db, doc, version)
        return browse.search(db, doc, v, q, field, limit)

    @app.post("/selections", response_model=SelectionOut, status_code=201)
    def create_selection(body: SelectionIn, db: Session = Depends(get_db)):
        sel, _created = browse.create_selection(db, body.name, body.node_ids, body.include_descendants)
        return browse.selection_detail(db, sel)

    @app.get("/selections/{selection_id}", response_model=SelectionOut)
    def get_selection(selection_id: str, db: Session = Depends(get_db)):
        return browse.selection_detail(db, browse.get_selection(db, selection_id))


app = None  # created lazily by `uvicorn app.main:create_app --factory`
