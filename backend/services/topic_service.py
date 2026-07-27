"""
Topic Service — clusters activities into topics using mem0 semantic search.

Each activity is assigned to an existing topic (if semantically similar) or spawns
a new one. Topics accumulate context over time and can be retrieved for chat enrichment.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ActivityLog, ActivityTopic

logger = structlog.get_logger()

# Minimum similarity score (0-1) to consider a mem0 match relevant
TOPIC_MATCH_THRESHOLD = 0.5

# Only match topics active within the last N days
TOPIC_RECENCY_DAYS = 7

async def assign_topic(activity: ActivityLog, session: AsyncSession) -> UUID | None:
    """Assign an activity to a topic. Creates a new topic if no match found.
    Returns the topic_id."""
    user_id = activity.user_id
    search_text = f"{activity.title} {activity.summary or ''}".strip()

    if not search_text:
        return None

    # 1. Search mem0 for similar content
    matched_mem0_id = None
    matched_score = 0.0
    try:
        from services.graphiti_service import search_memories
        memories = await search_memories(str(user_id), search_text, limit=5)
        for mem in memories:
            score = mem.get("score", 0)
            if score >= TOPIC_MATCH_THRESHOLD:
                matched_mem0_id = mem.get("id")
                matched_score = score
                break
    except Exception as e:
        logger.warning("mem0 search failed during topic assignment", error=str(e))

    # 2. Search recent topics in DB
    cutoff = datetime.now(timezone.utc) - timedelta(days=TOPIC_RECENCY_DAYS)
    result = await session.execute(
        select(ActivityTopic).where(
            ActivityTopic.user_id == user_id,
            ActivityTopic.last_activity_at >= cutoff,
        ).order_by(ActivityTopic.last_activity_at.desc()).limit(20)
    )
    recent_topics = result.scalars().all()

    # 3. If mem0 matched and we have a topic linked to that mem0 memory → reuse it
    if matched_mem0_id:
        for topic in recent_topics:
            if topic.mem0_memory_id == matched_mem0_id:
                # Update existing topic
                topic.last_activity_at = datetime.now(timezone.utc)
                topic.activity_count = (topic.activity_count or 0) + 1
                topic.updated_at = datetime.now(timezone.utc)
                activity.topic_id = topic.id
                await session.flush()
                return topic.id

    # 4. Try simple title-based matching against recent topics as fallback
    activity_title_lower = activity.title.lower()
    for topic in recent_topics:
        topic_title_lower = topic.title.lower()
        # Check if there's significant word overlap
        topic_words = set(topic_title_lower.split())
        activity_words = set(activity_title_lower.split())
        common = topic_words & activity_words
        # Remove stop words from overlap calculation
        stop_words = {"the", "a", "an", "in", "on", "at", "to", "for", "of", "with", "is", "are", "was", "re", "no"}
        meaningful_common = common - stop_words
        meaningful_topic = topic_words - stop_words
        if meaningful_topic and len(meaningful_common) >= max(2, len(meaningful_topic) * 0.5):
            topic.last_activity_at = datetime.now(timezone.utc)
            topic.activity_count = (topic.activity_count or 0) + 1
            topic.updated_at = datetime.now(timezone.utc)
            activity.topic_id = topic.id
            await session.flush()
            return topic.id

    # 5. No match — create a new topic.
    #
    # We used to also push the activity title+summary into Graphiti here.
    # That produced a firehose of low-value "User reported ticket X" /
    # "User viewed Y" nodes that drowned the graph in transient episodic
    # events. Memory now only comes from durable, explicitly-user-originated
    # signals: the chat loop (`add_memories_from_conversation`), manual
    # saves via the Memory page, and the import endpoint. Activities
    # continue to be tracked in `activity_log` + clustered into
    # `activity_topics` below — we just don't promote them to Graphiti.
    new_topic = ActivityTopic(
        user_id=user_id,
        title=activity.title[:200],
        summary=f"[{activity.source}] {activity.event_type}: {activity.title}"[:500],
        last_activity_at=datetime.now(timezone.utc),
        activity_count=1,
    )

    session.add(new_topic)
    await session.flush()

    activity.topic_id = new_topic.id
    await session.flush()

    return new_topic.id


async def assign_topics_for_user(user_id: UUID, session: AsyncSession) -> int:
    """Assign topics to all unassigned activities for a user. Returns count assigned."""
    result = await session.execute(
        select(ActivityLog).where(
            ActivityLog.user_id == user_id,
            ActivityLog.topic_id.is_(None),
        ).order_by(ActivityLog.created_at.asc()).limit(100)
    )
    activities = result.scalars().all()

    assigned = 0
    for activity in activities:
        try:
            topic_id = await assign_topic(activity, session)
            if topic_id:
                assigned += 1
        except Exception as e:
            logger.warning(
                "Failed to assign topic to activity",
                activity_id=str(activity.id),
                error=str(e),
            )
            continue

    return assigned


async def get_topic_context(
    user_id: str, query: str, session: AsyncSession
) -> list[dict]:
    """Get relevant topic context for a chat query.
    Returns a list of topic dicts with their recent activities."""
    # 1. Search mem0 for relevant memories/topics
    matched_topic_ids = set()
    try:
        from services.graphiti_service import search_memories
        memories = await search_memories(user_id, query, limit=10)
        if memories:
            # Find topics linked to matched mem0 memories
            mem0_ids = [m.get("id") for m in memories if m.get("id")]
            if mem0_ids:
                result = await session.execute(
                    select(ActivityTopic).where(
                        ActivityTopic.user_id == user_id,
                        ActivityTopic.mem0_memory_id.in_(mem0_ids),
                    )
                )
                for topic in result.scalars().all():
                    matched_topic_ids.add(topic.id)
    except Exception as e:
        logger.warning("mem0 topic search failed", error=str(e))

    # 2. Also search topics by title keyword match — titles are encrypted, so load recent + Python filter
    words = [w.lower() for w in query.split()[:5] if len(w) > 2]
    if words:
        result = await session.execute(
            select(ActivityTopic).where(
                ActivityTopic.user_id == user_id,
            ).order_by(ActivityTopic.last_activity_at.desc()).limit(200)
        )
        matched = 0
        for topic in result.scalars():
            if matched >= 5:
                break
            title_lower = (topic.title or "").lower()
            if any(w in title_lower for w in words):
                matched_topic_ids.add(topic.id)
                matched += 1

    if not matched_topic_ids:
        return []

    # 3. Load recent activities for each matched topic
    topic_contexts = []
    for topic_id in list(matched_topic_ids)[:5]:
        topic_result = await session.execute(
            select(ActivityTopic).where(ActivityTopic.id == topic_id)
        )
        topic = topic_result.scalar_one_or_none()
        if not topic:
            continue

        activities_result = await session.execute(
            select(ActivityLog).where(
                ActivityLog.topic_id == topic_id,
            ).order_by(ActivityLog.created_at.desc()).limit(10)
        )
        activities = activities_result.scalars().all()

        topic_contexts.append({
            "topic_id": str(topic.id),
            "title": topic.title,
            "summary": topic.summary,
            "activity_count": topic.activity_count,
            "last_activity_at": topic.last_activity_at.isoformat() if topic.last_activity_at else None,
            "activities": [
                {
                    "source": a.source,
                    "event_type": a.event_type,
                    "title": a.title,
                    "summary": a.summary,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in activities
            ],
        })

    return topic_contexts
