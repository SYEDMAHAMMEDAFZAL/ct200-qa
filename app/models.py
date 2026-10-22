"""Relational model: documents -> versions -> nodes, plus logical nodes and selections.

A *Node* is one section as it exists in one version (immutable once ingested).
A *LogicalNode* is the identity of "the same section" across versions; every
Node points at one. Selections pin concrete Node ids, so they always resolve
to the exact text they were created against.
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(Text, default="")
    versions: Mapped[list["DocumentVersion"]] = relationship(back_populates="document",
                                                             order_by="DocumentVersion.version_number")


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version_number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    version_number: Mapped[int] = mapped_column(Integer)
    source_filename: Mapped[str] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text, default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    parse_warnings: Mapped[list] = mapped_column(JSON, default=list)
    change_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    document: Mapped[Document] = relationship(back_populates="versions")


class LogicalNode(Base):
    __tablename__ = "logical_nodes"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    first_version_number: Mapped[int] = mapped_column(Integer)
    first_section_key: Mapped[str] = mapped_column(String(64))


class Node(Base):
    __tablename__ = "nodes"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # "<slug>:v<N>:<key>"
    version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"), index=True)
    logical_id: Mapped[int] = mapped_column(ForeignKey("logical_nodes.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    key: Mapped[str] = mapped_column(String(64))
    section_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heading: Mapped[str] = mapped_column(Text)
    level: Mapped[int] = mapped_column(Integer)
    order_index: Mapped[int] = mapped_column(Integer)
    page: Mapped[int] = mapped_column(Integer)
    body_text: Mapped[str] = mapped_column(Text, default="")
    blocks: Mapped[list] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    anomalies: Mapped[list] = mapped_column(JSON, default=list)
    references: Mapped[list] = mapped_column(JSON, default=list)
    # relation to the previous version of the same logical node
    change_status: Mapped[str] = mapped_column(String(16))  # new | unchanged | modified
    match_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prev_node_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    version: Mapped[DocumentVersion] = relationship()


class Selection(Base):
    __tablename__ = "selections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)  # hash of pinned node ids
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    items: Mapped[list["SelectionItem"]] = relationship(order_by="SelectionItem.position",
                                                        cascade="all, delete-orphan")


class SelectionItem(Base):
    __tablename__ = "selection_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    selection_id: Mapped[str] = mapped_column(ForeignKey("selections.id"), index=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    position: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))  # hash at pin time, re-checked on read
    node: Mapped[Node] = relationship()
