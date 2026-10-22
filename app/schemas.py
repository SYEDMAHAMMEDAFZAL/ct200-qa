"""API request/response models."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class VersionOut(BaseModel):
    version_number: int
    source_filename: str
    file_sha256: str
    title: str
    page_count: int
    created_at: datetime
    parse_warnings: list[dict]
    change_summary: dict


class IngestOut(BaseModel):
    created: bool = Field(description="false when the uploaded bytes equal the latest version")
    version: VersionOut


class NodeSummary(BaseModel):
    id: str
    section_number: str | None
    heading: str
    level: int
    page: int
    content_hash: str
    change_status: str
    child_count: int | None = 0
    anomalies: list[dict] = []


class NodeDetail(NodeSummary):
    document: str
    version: int
    parent_id: str | None
    breadcrumb: list[str]
    body_text: str
    blocks: list[dict]
    references: list[str]
    logical_id: int
    match_method: str | None
    prev_node_id: str | None
    children: list[NodeSummary]


class SearchHit(NodeSummary):
    version: int
    matched_in: list[str]
    snippet: str


class SelectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    node_ids: list[str] = Field(min_length=1, max_length=50)
    include_descendants: bool = Field(
        default=False, description="expand each node to its whole subtree at creation time (pinned explicitly)")


class SelectionItemOut(BaseModel):
    node_id: str
    version: int
    section_number: str | None
    heading: str
    content_hash: str
    pinned_text: str
    pin_intact: bool = Field(description="stored node text still hashes to the value captured at pin time")
    latest: dict = Field(description="where this logical section stands in the latest document version")


class SelectionOut(BaseModel):
    id: str
    name: str
    document: str
    created_at: datetime
    versions: list[int]
    warnings: list[str] = []
    items: list[SelectionItemOut]


class GenerateIn(BaseModel):
    force: bool = Field(default=False, description="create a new generation even if one already exists")
