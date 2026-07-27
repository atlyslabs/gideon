"""
Memory Router — CRUD endpoints for user memories.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncio
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from db.models import Memory
from middleware.auth import get_current_user
from services.auth_service import AuthUser
from services.user_service import get_or_create_user

logger = structlog.get_logger()

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class MemoryResponse(BaseModel):
    id: UUID
    content: str
    category: str
    source: str
    source_conversation_id: Optional[str] = None
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class MemoryListResponse(BaseModel):
    memories: list[MemoryResponse]
    count: int


class MemoryCreate(BaseModel):
    content: str
    category: Optional[str] = "general"


class MemoryUpdate(BaseModel):
    content: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    category: Optional[str] = Query(None, description="Filter by category"),
    search: Optional[str] = Query(None, description="Search content"),
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """List all active memories for the current user.

    Tries mem0 first (if available and no category filter), falls back to DB.
    """
    user = await get_or_create_user(session, auth_user)

    # Try Graphiti for the full list (no category filter in Graphiti)
    if not category and not search:
        from services.graphiti_service import get_all_memories as mem0_list
        mem0_memories = await mem0_list(str(user.id))
        if mem0_memories:
            return MemoryListResponse(
                memories=[
                    MemoryResponse(
                        id=m.get("id", ""),
                        content=m.get("content", ""),
                        category=m.get("category", "general"),
                        source=m.get("source", "mem0"),
                        source_conversation_id=None,
                        is_active=True,
                        created_at=m.get("created_at"),
                        updated_at=m.get("updated_at"),
                    )
                    for m in mem0_memories
                ],
                count=len(mem0_memories),
            )

    # Fallback: DB query
    stmt = select(Memory).where(
        Memory.user_id == user.id,
        Memory.is_active == True,
    )
    if category:
        stmt = stmt.where(Memory.category == category)
    if search:
        stmt = stmt.where(Memory.content.ilike(f"%{search}%"))
    stmt = stmt.order_by(Memory.created_at.desc())

    result = await session.execute(stmt)
    memories = result.scalars().all()

    return MemoryListResponse(
        memories=[
            MemoryResponse(
                id=m.id,
                content=m.content,
                category=m.category,
                source=m.source,
                source_conversation_id=m.source_conversation_id,
                is_active=m.is_active,
                created_at=m.created_at,
                updated_at=m.updated_at,
            )
            for m in memories
        ],
        count=len(memories),
    )


class MemorySearchResult(BaseModel):
    content: str
    score: float = 0.0
    id: Optional[str] = None
    category: Optional[str] = None
    created_at: Optional[str] = None


class MemorySearchResponse(BaseModel):
    results: list[MemorySearchResult]
    count: int


@router.get("/search", response_model=MemorySearchResponse)
async def search_memories(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(10, ge=1, le=50),
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Semantic search across memories. Uses mem0 if available, falls back to DB keyword search."""
    user = await get_or_create_user(session, auth_user)

    # Try mem0 semantic search first
    from services.graphiti_service import search_memories as mem0_search
    mem0_results = await mem0_search(str(user.id), q, limit=limit)
    if mem0_results:
        return MemorySearchResponse(
            results=[
                MemorySearchResult(
                    content=r.get("content", ""),
                    score=r.get("score", 0.0),
                    id=r.get("id"),
                    category=r.get("category"),
                    created_at=r.get("created_at"),
                )
                for r in mem0_results
            ],
            count=len(mem0_results),
        )

    # Fallback: DB keyword search
    from sqlalchemy import or_
    words = q.split()
    stmt = select(Memory).where(
        Memory.user_id == user.id,
        Memory.is_active == True,
    )
    if words:
        stmt = stmt.where(or_(*[Memory.content.ilike(f"%{w}%") for w in words]))
    stmt = stmt.order_by(Memory.updated_at.desc()).limit(limit)
    result = await session.execute(stmt)
    db_memories = result.scalars().all()

    return MemorySearchResponse(
        results=[
            MemorySearchResult(
                content=m.content,
                score=0.0,
                id=str(m.id),
                category=m.category,
                created_at=m.created_at.isoformat() if m.created_at else None,
            )
            for m in db_memories
        ],
        count=len(db_memories),
    )


class GraphNode(BaseModel):
    id: str
    name: str
    summary: str = ""
    labels: list[str] = []
    kind: str = "other"
    degree: int = 0


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    fact: str
    created_at: str = ""


class MemoryGraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


@router.get("/graph", response_model=MemoryGraphResponse)
async def memory_graph(
    limit: int = Query(500, ge=1, le=2000),
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Return the user's Graphiti memory as a nodes + edges graph.
    Powers the interactive Memory Graph visualization."""
    user = await get_or_create_user(session, auth_user)
    from services.graphiti_service import get_memory_graph
    graph = await get_memory_graph(str(user.id), limit=limit)
    return MemoryGraphResponse(
        nodes=[GraphNode(**n) for n in graph.get("nodes", [])],
        edges=[GraphEdge(**e) for e in graph.get("edges", [])],
    )


class GraphAddRequest(BaseModel):
    content: str


# Module-level strong refs so asyncio doesn't GC fire-and-forget ingest tasks
# (ASYNC300).
_background_ingest_tasks: set[asyncio.Task] = set()


async def _ingest_memory_async(user_id: str, content: str, user_name: str | None) -> None:
    """Run Graphiti's LLM-backed entity+edge extraction off the request path.
    Takes 1-5s; the endpoint returns 202 immediately. Only tracks the event
    if the ingest actually landed a memory — failed ingests and low-signal
    content (dropped by the preflight filter) shouldn't inflate the
    memory_created counter."""
    from services.graphiti_service import save_memory
    try:
        result = await save_memory(user_id, content, user_name=user_name)
    except Exception as e:
        logger.warning("Background memory ingest failed", user_id=user_id, error=str(e))
        return
    if result is not None:
        from services.events_service import track_memory_created
        track_memory_created(user_id, source="manual")


@router.post("/graph/add", status_code=202)
async def memory_graph_add(
    body: GraphAddRequest,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Queue a new memory for background ingestion into Graphiti.
    Returns 202 immediately; the node/edges appear in the graph once the
    LLM extraction completes (~1-5s)."""
    user = await get_or_create_user(session, auth_user)
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content cannot be empty.")
    if len(content) > 4000:
        raise HTTPException(status_code=400, detail="Memory too long (max 4000 chars).")

    task = asyncio.create_task(_ingest_memory_async(str(user.id), content, user.name or None))
    _background_ingest_tasks.add(task)
    task.add_done_callback(_background_ingest_tasks.discard)

    return {"queued": True}


@router.delete("/graph/node/{node_id}")
async def memory_graph_delete_node(
    node_id: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Forget an entity and every fact touching it. Scoped to the user's
    group_id — UUIDs are global but a user can't delete another user's node."""
    user = await get_or_create_user(session, auth_user)
    from services.graphiti_service import delete_entity
    ok = await delete_entity(node_id, str(user.id))
    if not ok:
        raise HTTPException(status_code=404, detail="Entity not found.")

    from services.events_service import track_memory_deleted
    track_memory_deleted(str(user.id))
    return {"success": True}


@router.post("", response_model=MemoryResponse, status_code=201)
async def create_memory(
    body: MemoryCreate,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Manually create a memory."""
    user = await get_or_create_user(session, auth_user)

    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content cannot be empty.")

    # Check max 100 per user
    count_result = await session.execute(
        select(func.count(Memory.id)).where(
            Memory.user_id == user.id,
            Memory.is_active == True,
        )
    )
    count = count_result.scalar()
    if count >= 100:
        raise HTTPException(status_code=400, detail="Memory limit reached (100). Delete some memories first.")

    # Dedup check
    dup_result = await session.execute(
        select(Memory).where(
            Memory.user_id == user.id,
            Memory.is_active == True,
            Memory.content.ilike(f"%{content}%"),
        )
    )
    if dup_result.scalars().first():
        raise HTTPException(status_code=409, detail="A similar memory already exists.")

    memory = Memory(
        user_id=user.id,
        content=content,
        category=body.category or "general",
        source="manual",
    )
    session.add(memory)
    await session.flush()
    await session.refresh(memory)

    from services.events_service import track_memory_created
    track_memory_created(str(user.id), source="manual")
    return MemoryResponse(
        id=memory.id,
        content=memory.content,
        category=memory.category,
        source=memory.source,
        source_conversation_id=memory.source_conversation_id,
        is_active=memory.is_active,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


@router.patch("/{memory_id}", response_model=MemoryResponse)
async def update_memory(
    memory_id: UUID,
    body: MemoryUpdate,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Update a memory's content."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Memory).where(
            Memory.id == memory_id,
            Memory.user_id == user.id,
            Memory.is_active == True,
        )
    )
    memory = result.scalar_one_or_none()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found.")

    memory.content = body.content.strip()
    await session.flush()
    await session.refresh(memory)

    return MemoryResponse(
        id=memory.id,
        content=memory.content,
        category=memory.category,
        source=memory.source,
        source_conversation_id=memory.source_conversation_id,
        is_active=memory.is_active,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Delete a memory. Tries mem0 first, then falls back to DB soft-delete."""
    user = await get_or_create_user(session, auth_user)

    # Try mem0 first
    from services.graphiti_service import delete_memory as mem0_delete
    mem0_success = await mem0_delete(memory_id, str(user.id))
    if mem0_success:
        from services.events_service import track_memory_deleted
        track_memory_deleted(str(user.id))
        return {"success": True, "message": "Memory deleted."}

    # Fallback: DB soft-delete
    try:
        uuid_id = UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Memory not found.")

    result = await session.execute(
        select(Memory).where(
            Memory.id == uuid_id,
            Memory.user_id == user.id,
            Memory.is_active == True,
        )
    )
    memory = result.scalar_one_or_none()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found.")

    memory.is_active = False
    await session.flush()

    from services.events_service import track_memory_deleted
    track_memory_deleted(str(user.id))
    return {"success": True, "message": "Memory deleted."}


# ---------------------------------------------------------------------------
# Bulk import
# ---------------------------------------------------------------------------

class MemoryImportRequest(BaseModel):
    text: str



def _parse_import_text(text: str) -> list[str]:
    """Split pasted text into individual memory lines. Each non-empty line is a memory."""
    lines = []
    for line in text.strip().split("\n"):
        line = line.strip().strip("`")
        if line and len(line) > 5:
            lines.append(line)
    return lines


@router.post("/import")
async def import_memories(
    body: MemoryImportRequest,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Bulk import memories from pasted text (e.g. exported from ChatGPT/Claude)."""
    user = await get_or_create_user(session, auth_user)

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text cannot be empty.")

    entries = _parse_import_text(text)
    if not entries:
        raise HTTPException(status_code=400, detail="Could not parse any memories from the text.")

    # Push each memory through the Graphiti pipeline (preflight filter +
    # subject rewrite + typed entity extraction)
    from services.graphiti_service import save_memory as mem0_save

    imported = 0
    failed = 0
    for line in entries[:200]:
        result = await mem0_save(str(user.id), line, user_name=user.name or None)
        if result is not None:
            imported += 1
        else:
            failed += 1

    return {"imported": imported, "total_parsed": len(entries), "failed": failed}
