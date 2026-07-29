"""
Graphiti Service — self-hosted semantic + temporal memory layer for Gideon.

Backed by Graphiti on Neo4j (point NEO4J_URI at your instance). Runs alongside
mem0_service during the migration so callers can dual-write / dual-read and
compare retrieval quality before cutover.

Public API mirrors mem0_service so call-sites can swap import lines and nothing
else. Each user's memories live in a dedicated Graphiti `group_id` (= user UUID
as string), so search and deletion are per-user by construction.

All functions fail soft: any exception is logged and we return an empty result.
Callers already treat memory as best-effort.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

import structlog

from config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER
from services.graphiti_types import ENTITY_TYPES

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Preflight filters — keep junk out of the graph.
# Applied on every save_memory call AND on the backfill script before
# re-extraction, so the forward path and historical cleanup use the same
# definition of "signal vs. noise".
# ---------------------------------------------------------------------------

# Node-name / whole-content shapes that are NEVER durable entities: ticket
# ids, raw URLs, dates as subjects, phone numbers, bare emails, hashes.
_JUNK_CONTENT_PATTERNS = [
    re.compile(r"^[A-Z]{2,}-\d+$"),
    re.compile(r"^https?://", re.IGNORECASE),
    re.compile(r"^\+?\d[\d\s\-()]{6,}$"),
    re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    # Pure ISO date or datetime with nothing else. "2026-04-15" rejected,
    # "2026-04-15 is the team offsite" kept.
    re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+\-]\d{2}:?\d{2})?)?$"),
    re.compile(r"^[a-f0-9]{16,}$", re.IGNORECASE),
]

# Transactional verbs — signal an event, not a durable fact. "User reports
# X" / "User viewed Y" shouldn't be memories.
_TRANSACTIONAL_VERBS = frozenset({
    "reports", "reported",
    "views", "viewed",
    "opens", "opened",
    "receives", "received",
    "sees", "saw",
    "reads", "read",
    "clicks", "clicked",
    "visits", "visited",
    "checked",
    "looked",
    "posted",
    "sent",
    "mentioned",
    "requests", "requested",
})

# Episodic-desire / in-progress phrasing that can appear mid-sentence.
# Catches "User wants to perform a search", "tried to send", "was drafting".
# Stative preferences ("prefers X", "wants LinkedIn access") do NOT match —
# the key is the "to <verb>" or "-ing" form that forces an action.
_EPISODIC_PATTERNS = [
    re.compile(r"\b(wants|wanted|needs|needed|tried|attempted|asked) to \w+", re.IGNORECASE),
    re.compile(r"\bis (trying|looking|planning) to \w+", re.IGNORECASE),
    re.compile(r"\b(was|were) \w+ing\b", re.IGNORECASE),
]

# Raw identifier markers — content pinned to a specific record id is a
# one-off lookup ("status of user_id=123"), not a durable fact about the
# user. Extend this list with your own id conventions.
_RAW_ID_MARKERS = [
    re.compile(r"\buser_id\s*=", re.IGNORECASE),
    re.compile(r"\baccount_id\s*=", re.IGNORECASE),
    re.compile(r"\bcustomer_id\s*=", re.IGNORECASE),
    re.compile(r"\buid\s*=\s*\w", re.IGNORECASE),
]

_MIN_CONTENT_LENGTH = 20


def is_low_signal(content: str) -> bool:
    """Return True if the content is too noisy to store as a durable memory.
    See module-level docstring for the categories."""
    if not content:
        return True
    stripped = content.strip()
    if len(stripped) < _MIN_CONTENT_LENGTH:
        return True
    if not any(c.isalpha() for c in stripped):
        return True
    for pat in _JUNK_CONTENT_PATTERNS:
        if pat.match(stripped):
            return True
    for pat in _RAW_ID_MARKERS:
        if pat.search(stripped):
            return True
    for pat in _EPISODIC_PATTERNS:
        if pat.search(stripped):
            return True
    # Transactional test: after the first whitespace, is the second token a
    # transactional verb? Captures "User reports X", "User viewed Y".
    tokens = stripped.split(None, 2)
    if len(tokens) >= 2 and tokens[1].lower().strip(",.:;") in _TRANSACTIONAL_VERBS:
        return True
    return False


# Precompiled subject-rewrite patterns — replaced BEFORE sending to Graphiti
# so the LLM extractor sees the user's actual name as the subject, not the
# generic "User" placeholder that ChatGPT-style exports use.
_USER_WORD = re.compile(r"\bUser\b")
_LOWER_USER_WORD = re.compile(r"\buser\b")
_THE_USER = re.compile(r"\bthe user\b", re.IGNORECASE)


async def classify_durability_haiku(
    facts: list[str],
    user_name: str,
    batch_size: int = 20,
) -> list[bool]:
    """Ask Haiku to classify each fact as durable (True) or episodic (False).

    A durable memory is a fact likely to still be true months from now:
    preferences, relationships, skills, work context. An episodic memory is a
    transient event: a one-off request, a status update, a ticket mention, a
    just-did-X action. We drop episodic facts before re-ingestion.

    Batched to amortize the Haiku call overhead. Returns one bool per input
    fact (same order). On any error we conservatively keep the batch — the
    regex filter has already done the cheap noise removal.
    """
    if not facts:
        return []
    # Defer the import so the rest of graphiti_service doesn't pay the
    # llm import cost on every startup.
    from dataclasses import replace as _replace
    from services.llm import get_spec, llm

    keep: list[bool] = []
    for start in range(0, len(facts), batch_size):
        chunk = facts[start:start + batch_size]
        numbered = "\n".join(f"{i + 1}. {fact}" for i, fact in enumerate(chunk))
        prompt = f"""You are filtering facts for {user_name}'s personal memory graph.

For each numbered fact below, answer YES if it's a DURABLE memory — something likely to still be true about {user_name} months from now: preferences, relationships, roles, projects they lead, companies they work with, skills, habits, recurring patterns.

Answer NO if it's EPISODIC — a one-time event, transient request, status update, just-did-X action, or references to a specific ticket/customer/session that won't matter later.

Examples:
- "{user_name} prefers terse responses" → YES
- "{user_name} works on the Payments team" → YES
- "{user_name} leads the pricing refresh project" → YES
- "{user_name} reported ticket ENG-6135" → NO
- "{user_name} requested a deep dive into customer user_id=abc" → NO
- "{user_name} wants to perform a search on LinkedIn" → NO

Output EXACTLY {len(chunk)} lines, one per fact, in the same order, each line just "YES" or "NO" — nothing else.

Facts:
{numbered}"""

        try:
            classifier_spec = _replace(get_spec("suggestions"), max_tokens=len(chunk) * 6)
            response = await llm.complete(
                classifier_spec,
                messages=[{"role": "user", "content": prompt}],
                timeout=150.0,  # was 60s — batch durability classify on Kimi reasoning
            )
            text = response.text
            lines = [line.strip().upper() for line in text.strip().split("\n") if line.strip()]
            # Pad / truncate to len(chunk) — defensive against ragged output
            decisions = [line.startswith("YES") for line in lines[:len(chunk)]]
            while len(decisions) < len(chunk):
                # Missing line = conservative KEEP
                decisions.append(True)
            keep.extend(decisions)
        except Exception as e:
            logger.warning("graphiti: durability classifier failed, keeping batch",
                           batch_start=start, error=str(e))
            keep.extend([True] * len(chunk))

    return keep


def rewrite_subject(content: str, user_name: str | None) -> str:
    """Replace generic "User" / "the user" references with the user's actual
    name so Graphiti's entity extractor attaches facts to `:Person
    {name: <actual>}` instead of creating a giant `User` hub.

    Skips first-person pronouns ("I") because chat transcripts contain
    other people's "I" too — rewriting those would misattribute facts.

    Uses a lambda replacement so the name is inserted verbatim — `re.sub`
    otherwise treats `\\1`, `\\\\`, etc. in the replacement string as
    backreferences and raises `re.error: bad escape` on names that contain
    backslashes or unusual metacharacters.

    No-op when user_name is empty (used as a fallback during migration).
    """
    if not user_name:
        return content
    sub = lambda _m: user_name  # noqa: E731 — trivial closure, not worth a def
    out = content
    out = _THE_USER.sub(sub, out)
    out = _USER_WORD.sub(sub, out)
    out = _LOWER_USER_WORD.sub(sub, out)
    return out


_graphiti: Any | None = None
_graphiti_lock = asyncio.Lock()
_graphiti_available = True


def _parse_bolt_host(uri: str) -> tuple[str, int]:
    """Extract host + port from a Neo4j Bolt URI like 'bolt://host:7687' or
    'neo4j://host:7687'. Falls back to sensible defaults on any parse issue.
    """
    from urllib.parse import urlparse
    try:
        parsed = urlparse(uri)
        host = parsed.hostname or "localhost"
        port = parsed.port or 7687
        return host, port
    except Exception:
        return "localhost", 7687


async def _neo4j_reachable(uri: str, timeout: float = 1.0) -> bool:
    """Quick TCP probe to the Bolt port. Returns False if the port refuses
    connection within ``timeout`` seconds. This runs BEFORE Graphiti init so
    the neo4j driver's internal retry loops never start when the host is down.
    """
    host, port = _parse_bolt_host(uri)
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
        return False


async def _get_graphiti() -> Any | None:
    """Lazy-init Graphiti singleton. Returns None if init failed (caller treats as disabled)."""
    global _graphiti, _graphiti_available
    if _graphiti is not None:
        return _graphiti
    if not _graphiti_available:
        return None
    async with _graphiti_lock:
        if _graphiti is not None:
            return _graphiti
        if not _graphiti_available:
            return None
        # Guard: refuse to connect if the password is unset. Also refuse the
        # literal "testpassword" placeholder for any non-localhost host — a
        # kubectl port-forward to prod listens on localhost so the TCP probe
        # below won't tell us apart. Blocking testpassword when the host
        # resolves to something other than loopback prevents accidentally
        # spamming a real Neo4j with bad auth and tripping its rate-limit.
        if not NEO4J_PASSWORD:
            logger.info(
                "Graphiti skipped — NEO4J_PASSWORD is unset, memory features disabled",
                uri=NEO4J_URI,
            )
            _graphiti_available = False
            return None
        if NEO4J_PASSWORD == "testpassword":
            host, _ = _parse_bolt_host(NEO4J_URI)
            if host not in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
                logger.info(
                    "Graphiti skipped — refusing placeholder password against non-localhost host",
                    uri=NEO4J_URI,
                    host=host,
                )
                _graphiti_available = False
                return None
        # TCP probe before touching Graphiti — the neo4j driver spawns its own
        # background transaction retry loops that outlive our `wait_for` cancel,
        # so if we instantiate Graphiti against an unreachable host we get
        # minutes of "Transaction failed and will be retried" spam on stderr
        # that we can't cancel. Preflight-probe the bolt port (default 7687)
        # with a 1s connect timeout and skip entirely if nothing answers.
        if not await _neo4j_reachable(NEO4J_URI):
            logger.info(
                "Graphiti skipped — Neo4j unreachable, memory features disabled",
                uri=NEO4J_URI,
            )
            _graphiti_available = False
            return None

        client = None
        try:
            from graphiti_core import Graphiti
            client = Graphiti(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
            await asyncio.wait_for(client.build_indices_and_constraints(), timeout=5.0)
            _graphiti = client
            logger.info("Graphiti initialized", uri=NEO4J_URI)
            return _graphiti
        except asyncio.TimeoutError:
            logger.warning(
                "Graphiti init timed out — memory disabled. Neo4j unreachable at %s",
                NEO4J_URI,
            )
            if client is not None:
                try:
                    await client.close()  # stop the driver's in-flight retry loops
                except Exception:
                    pass
            _graphiti_available = False
            return None
        except Exception as e:
            logger.warning("Graphiti init failed — memory disabled", error=str(e))
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
            _graphiti_available = False
            return None


def _messages_to_text(messages: list[dict]) -> str:
    """Flatten an OpenAI-style message list into a single plain-text episode body."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
            )
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def add_memories_from_conversation(
    user_id: str,
    messages: list[dict],
    user_name: str | None = None,
) -> list[dict]:
    """Extract and store memories from a conversation. Returns a single-element list
    describing the episode ingested (for caller-logging purposes).

    When ``user_name`` is provided we rewrite generic "User" subjects to the
    actual name and steer the extractor via ``source_description``. We also
    pass the typed entity schemas so Graphiti emits :Person / :Project /
    :Preference labels that the UI can color without regex."""
    try:
        client = await _get_graphiti()
        if not client:
            return []
        body = _messages_to_text(messages)
        if not body.strip():
            return []
        body = rewrite_subject(body, user_name)
        from graphiti_core.nodes import EpisodeType
        ref_time = datetime.now(timezone.utc)
        name = f"conv_{int(ref_time.timestamp())}"
        subject = user_name or f"user {user_id}"
        await client.add_episode(
            name=name,
            episode_body=body,
            source=EpisodeType.message,
            source_description=f"Conversation involving {subject} — extract durable facts about their preferences, relationships, and work context",
            reference_time=ref_time,
            group_id=user_id,
            entity_types=ENTITY_TYPES,
        )
        logger.info("graphiti: ingested episode", user_id=user_id, name=name)
        return [{"id": name, "content": body[:500]}]
    except Exception as e:
        logger.warning("graphiti: add_memories_from_conversation failed", user_id=user_id, error=str(e))
        return []


async def search_memories(
    user_id: str,
    query: str,
    limit: int = 10,
) -> list[dict]:
    """Hybrid semantic + graph search over the user's memory. Returns facts as
    `{id, content, score}` dicts — score defaults to 1.0 since Graphiti doesn't
    expose the hybrid score on every result type."""
    try:
        client = await _get_graphiti()
        if not client:
            return []
        edges = await client.search(
            query=query,
            group_ids=[user_id],
            num_results=limit,
        )
        results: list[dict] = []
        for e in edges or []:
            uuid = getattr(e, "uuid", None) or ""
            fact = getattr(e, "fact", None) or ""
            if not fact:
                continue
            raw_score = getattr(e, "hybrid_score", None)
            # `raw_score or 1.0` would turn a legitimate 0.0 into 1.0, so use
            # an explicit None check for the missing-attribute case.
            score = float(raw_score) if raw_score is not None else 1.0
            results.append({"id": uuid, "content": fact, "score": score})
        return results
    except Exception as e:
        logger.warning("graphiti: search failed", user_id=user_id, error=str(e))
        return []


async def get_all_memories(user_id: str) -> list[dict]:
    """List all facts (EntityEdges) Graphiti has learned for a user.
    Used by the Memory settings page."""
    try:
        client = await _get_graphiti()
        if not client:
            return []
        driver = getattr(client, "driver", None)
        if driver is None:
            return []
        async with driver.session() as session:
            result = await session.run(
                """
                MATCH ()-[r:RELATES_TO]->()
                WHERE r.group_id = $gid
                RETURN r.uuid AS uuid, r.fact AS fact, r.created_at AS created_at
                ORDER BY r.created_at DESC
                LIMIT 500
                """,
                gid=user_id,
            )
            rows = await result.data()
        return [
            {
                "id": row.get("uuid", ""),
                "content": row.get("fact", ""),
                "created_at": _format_ts(row.get("created_at")),
            }
            for row in rows
            if row.get("fact")
        ]
    except Exception as e:
        logger.warning("graphiti: get_all failed", user_id=user_id, error=str(e))
        return []


async def get_memory_graph(user_id: str, limit: int = 500) -> dict:
    """Return a nodes+edges view of the user's Graphiti graph for visualization.

    Nodes = `:Entity` vertices scoped to the user's group_id.
    Edges = `:RELATES_TO` relationships between them.
    We tally a per-node degree so the UI can size nodes by connectedness.
    Caller sees an empty graph on any failure — this endpoint is best-effort.
    """
    try:
        client = await _get_graphiti()
        if not client:
            return {"nodes": [], "edges": []}
        driver = getattr(client, "driver", None)
        if driver is None:
            return {"nodes": [], "edges": []}
        async with driver.session() as session:
            edges_result = await session.run(
                """
                MATCH (a:Entity {group_id: $gid})-[r:RELATES_TO {group_id: $gid}]->(b:Entity {group_id: $gid})
                RETURN a.uuid AS src_id, a.name AS src_name, a.summary AS src_summary, labels(a) AS src_labels,
                       b.uuid AS dst_id, b.name AS dst_name, b.summary AS dst_summary, labels(b) AS dst_labels,
                       r.uuid AS edge_id, r.fact AS fact, r.created_at AS created_at
                ORDER BY r.created_at DESC
                LIMIT $limit
                """,
                gid=user_id,
                limit=limit,
            )
            rows = await edges_result.data()

        node_map: dict[str, dict] = {}
        edges: list[dict] = []
        for row in rows:
            for prefix in ("src", "dst"):
                nid = row.get(f"{prefix}_id")
                if not nid:
                    continue
                if nid not in node_map:
                    labels = row.get(f"{prefix}_labels") or []
                    node_map[nid] = {
                        "id": nid,
                        "name": row.get(f"{prefix}_name") or "",
                        "summary": row.get(f"{prefix}_summary") or "",
                        "labels": labels,
                        "kind": _kind_from_labels(labels),
                        "degree": 0,
                    }
                node_map[nid]["degree"] += 1
            if row.get("edge_id") and row.get("src_id") and row.get("dst_id") and row.get("fact"):
                edges.append({
                    "id": row["edge_id"],
                    "source": row["src_id"],
                    "target": row["dst_id"],
                    "fact": row["fact"],
                    "created_at": _format_ts(row.get("created_at")),
                })
        return {"nodes": list(node_map.values()), "edges": edges}
    except Exception as e:
        logger.warning("graphiti: get_memory_graph failed", user_id=user_id, error=str(e))
        return {"nodes": [], "edges": []}


async def delete_entity(entity_id: str, user_id: str) -> bool:
    """Delete a single `:Entity` node and every relationship touching it,
    scoped to the user's group_id. Returns True only if a node owned by
    ``user_id`` was actually deleted.

    Used by the graph UI's "Forget this entirely" action to remove a
    person/project/etc. and all its facts in one call.
    """
    if not user_id:
        logger.warning("graphiti: delete_entity called without user_id", entity_id=entity_id)
        return False
    try:
        client = await _get_graphiti()
        if not client:
            return False
        driver = getattr(client, "driver", None)
        if driver is None:
            return False
        async with driver.session() as session:
            result = await session.run(
                "MATCH (n:Entity {uuid: $uuid, group_id: $gid}) DETACH DELETE n",
                uuid=entity_id,
                gid=user_id,
            )
            summary = await result.consume()
            return summary.counters.nodes_deleted > 0
    except Exception as e:
        logger.warning("graphiti: delete_entity failed", entity_id=entity_id, error=str(e))
        return False


async def delete_memory(memory_id: str, user_id: str) -> bool:
    """Delete a single fact (EntityEdge) by uuid, scoped to the user.
    Returns True only if a row owned by `user_id` was actually deleted — UUIDs
    are global but we must NOT let one user delete another user's edge even
    with a known UUID. Also False if the client/driver is unavailable."""
    if not user_id:
        logger.warning("graphiti: delete_memory called without user_id", memory_id=memory_id)
        return False
    try:
        client = await _get_graphiti()
        if not client:
            return False
        driver = getattr(client, "driver", None)
        if driver is None:
            return False
        async with driver.session() as session:
            result = await session.run(
                "MATCH ()-[r:RELATES_TO {uuid: $uuid, group_id: $gid}]->() DELETE r",
                uuid=memory_id,
                gid=user_id,
            )
            summary = await result.consume()
            return summary.counters.relationships_deleted > 0
    except Exception as e:
        logger.warning("graphiti: delete failed", memory_id=memory_id, error=str(e))
        return False


async def delete_all_memories(user_id: str) -> bool:
    """Delete everything Graphiti knows about the user: entities + edges + episodes."""
    try:
        client = await _get_graphiti()
        if not client:
            return False
        driver = getattr(client, "driver", None)
        if driver is None:
            return False
        async with driver.session() as session:
            await session.run(
                "MATCH (n {group_id: $gid}) DETACH DELETE n",
                gid=user_id,
            )
        return True
    except Exception as e:
        logger.warning("graphiti: delete_all failed", user_id=user_id, error=str(e))
        return False


async def save_memory(
    user_id: str,
    content: str,
    user_name: str | None = None,
) -> dict | None:
    """Manually save a single memory. Returns the episode id, or None if
    the memory was rejected by the preflight filter or the ingest failed.

    Pipeline, in order:
      1. is_low_signal() — drop junk (short, pure-numbers, transactional verbs, ticket ids)
      2. rewrite_subject() — swap generic "User" / "the user" for user_name
      3. add_episode with typed entities + identity-aware source_description

    `user_name` SHOULD be provided by the caller. When omitted we skip step 2
    and fall back to a generic source_description — the extractor will still
    work but quality is lower. Use this escape hatch only for backfill
    restore / tests where the name is intentionally blank.
    """
    try:
        if is_low_signal(content):
            logger.debug("graphiti: save_memory dropped low-signal content",
                         user_id=user_id, content_length=len(content))
            return None

        content_rewritten = rewrite_subject(content, user_name)

        client = await _get_graphiti()
        if not client:
            return None
        from graphiti_core.nodes import EpisodeType
        ref_time = datetime.now(timezone.utc)
        name = f"manual_{int(ref_time.timestamp() * 1000)}"
        subject = user_name or f"user {user_id}"
        await client.add_episode(
            name=name,
            episode_body=content_rewritten,
            source=EpisodeType.text,
            source_description=f"Memory about {subject} — their preferences, relationships, and work context",
            reference_time=ref_time,
            group_id=user_id,
            entity_types=ENTITY_TYPES,
        )
        return {"id": name, "content": content_rewritten}
    except Exception as e:
        logger.warning("graphiti: save failed", user_id=user_id, error=str(e))
        return None


_KIND_LABELS = ("Person", "Project", "Company", "Preference", "Topic")


def _kind_from_labels(labels: list[str]) -> str:
    """Derive a lowercase `kind` from a node's Neo4j labels. Used by the UI
    to color/icon nodes without regex guessing. Falls back to "other" when
    no typed label is present (older pre-backfill nodes)."""
    for lbl in _KIND_LABELS:
        if lbl in labels:
            return lbl.lower()
    return "other"


def _format_ts(val: Any) -> str:
    """Best-effort ISO string from a neo4j value (DateTime, str, None)."""
    if val is None:
        return ""
    try:
        if hasattr(val, "isoformat"):
            return val.isoformat()
        return str(val)
    except Exception:
        return ""
