"""Version-aware policy ingestion and lexical retrieval.

Markdown files are the source of truth. PostgreSQL/SQLite store immutable document metadata
and section chunks so retrieval can filter by effective dates and cite a stable version.
Embeddings are reserved as a nullable field for a future pgvector comparison.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.models import PolicyChunk, PolicyDocument

POLICY_DIR = Path(__file__).resolve().parents[2] / "policies"


def _parse_frontmatter(text: str) -> tuple[dict[str, str | None], str]:
    metadata: dict[str, str | None] = {"version": "1.0.0", "effective_from": "2026-01-01", "effective_to": None}
    if not text.startswith("---\n"):
        return metadata, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return metadata, text
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip().strip('"\'')
        metadata[key.strip()] = None if value.lower() in {"", "null", "none"} else value
    return metadata, parts[2].lstrip("\n")


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _date_key(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def load_policy_sections() -> list[dict]:
    sections: list[dict] = []
    for path in sorted(POLICY_DIR.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        metadata, text = _parse_frontmatter(raw)
        title_match = re.search(r"^# (.+)$", text, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else path.stem.replace("-", " ").title()
        current_heading = title
        body: list[str] = []
        for line in text.splitlines():
            if line.startswith("## "):
                if body:
                    sections.append({
                        "document": path.name, "source_uri": f"policies/{path.name}", "title": title,
                        "section": current_heading, "text": "\n".join(body).strip(),
                        "version": metadata.get("version") or "1.0.0",
                        "effective_from": _parse_date(metadata.get("effective_from")),
                        "effective_to": _parse_date(metadata.get("effective_to")),
                        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                    })
                current_heading, body = line[3:].strip(), []
            elif line.startswith("# "):
                continue
            else:
                body.append(line)
        if body:
            sections.append({
                "document": path.name, "source_uri": f"policies/{path.name}", "title": title,
                "section": current_heading, "text": "\n".join(body).strip(),
                "version": metadata.get("version") or "1.0.0",
                "effective_from": _parse_date(metadata.get("effective_from")),
                "effective_to": _parse_date(metadata.get("effective_to")),
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            })
    return [section for section in sections if section["text"]]


def ingest_policy_documents(db: Session) -> dict[str, int]:
    """Idempotently ingest versioned Markdown sections; changing a version in place is rejected."""
    sections = load_policy_sections()
    by_document: dict[str, list[dict]] = defaultdict(list)
    for section in sections:
        by_document[section["source_uri"]].append(section)

    for source_uri, doc_sections in by_document.items():
        first = doc_sections[0]
        document_id = hashlib.sha256(f"{source_uri}:{first['version']}".encode()).hexdigest()[:40]
        document = db.get(PolicyDocument, document_id)
        if document:
            if document.content_hash != first["content_hash"] or document.title != first["title"]:
                raise ValueError(
                    f"Policy {source_uri} changed without a version bump. Increment its frontmatter version before ingestion."
                )
            if _date_key(document.effective_from) != _date_key(first["effective_from"]):
                raise ValueError(f"Policy {source_uri} effective_from is immutable within a version.")
            current_end = _date_key(document.effective_to)
            incoming_end = _date_key(first["effective_to"])
            if current_end != incoming_end:
                if current_end is None and incoming_end is not None:
                    # Closing an old version is metadata-only; its policy body remains immutable.
                    document.effective_to = first["effective_to"]
                else:
                    raise ValueError(f"Policy {source_uri} effective_to cannot be reopened or changed once set.")
        if document is None:
            document = PolicyDocument(
                id=document_id, title=first["title"], version=first["version"],
                effective_from=first["effective_from"], effective_to=first["effective_to"],
                source_uri=source_uri, content_hash=first["content_hash"],
            )
            db.add(document)
            db.flush()
        for chunk in doc_sections:
            existing = db.query(PolicyChunk).filter_by(
                document_id=document_id, section=chunk["section"], chunk_index=0,
            ).one_or_none()
            if existing is None:
                chunk_id = hashlib.sha256(f"{document_id}:{chunk['section']}:0".encode()).hexdigest()[:40]
                db.add(PolicyChunk(
                    id=chunk_id, document_id=document_id, section=chunk["section"], content=chunk["text"],
                    chunk_index=0, metadata_json=json.dumps({"source_uri": source_uri, "version": first["version"]}),
                ))
    db.commit()
    return {"documents": len(by_document), "chunks": len(sections)}


def retrieve_policies(query: str, limit: int = 3, *, db: Session | None = None,
                      as_of: datetime | None = None) -> list[dict]:
    """Return lexical matches from the active policy version with citations.

    With no database session, this remains usable as a file-based local fallback. When a
    session is supplied, an empty active policy registry returns no evidence (no silent fallback).
    """
    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if db is None:
        sections = load_policy_sections()
        sections = [s for s in sections if s["effective_from"] <= now and
                    (s["effective_to"] is None or s["effective_to"] > now)]
    else:
        documents = db.query(PolicyDocument).filter(
            PolicyDocument.effective_from <= now,
            or_(PolicyDocument.effective_to.is_(None), PolicyDocument.effective_to > now),
        ).all()
        document_map = {document.id: document for document in documents}
        documents_by_family: dict[str, list[PolicyDocument]] = defaultdict(list)
        for document in documents:
            documents_by_family[document.title].append(document)
        conflicting_families = {family for family, versions in documents_by_family.items() if len(versions) > 1}
        chunks = db.query(PolicyChunk).filter(PolicyChunk.document_id.in_(list(document_map))).all() if document_map else []
        sections = [{
            "document": document_map[chunk.document_id].source_uri.rsplit("/", 1)[-1],
            "source_uri": document_map[chunk.document_id].source_uri,
            "title": document_map[chunk.document_id].title,
            "section": chunk.section,
            "text": chunk.content,
            "version": document_map[chunk.document_id].version,
            "effective_from": document_map[chunk.document_id].effective_from,
            "effective_to": document_map[chunk.document_id].effective_to,
            "document_id": document_map[chunk.document_id].id,
            "policy_conflict": document_map[chunk.document_id].title in conflicting_families,
        } for chunk in chunks]

    tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
    scored = []
    for section in sections:
        content_tokens = set(re.findall(r"[a-z0-9]+", f'{section["section"]} {section["text"]}'.lower()))
        overlap = len(tokens & content_tokens)
        if overlap:
            score = overlap / max(1, len(tokens))
            scored.append((score, section))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [{**section, "effective_from": section["effective_from"].isoformat() if section["effective_from"] else None,
             "effective_to": section["effective_to"].isoformat() if section["effective_to"] else None,
             "score": round(score, 3)} for score, section in scored[:limit]]
