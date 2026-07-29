"""
SQLAlchemy ORM models for Gideon service.

Tables: users, integrations, conversations, messages, skills, workflows, todos, memories
"""

from db.database import Base
from db.encrypted_types import EncryptedJSON, EncryptedText
from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PGUUID
from sqlalchemy.orm import backref, relationship


class User(Base):
    __tablename__ = "users"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    auth_provider_id = Column(String(255), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    name = Column(String(255))
    persona_text = Column(EncryptedText)
    timezone = Column(String(50), server_default=text("'UTC'"))
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    preferred_communication = Column(String(50), server_default=text("'gmail'"))
    city = Column(String(100), nullable=True)
    role = Column(String(100), nullable=True)
    department = Column(String(100), nullable=True)
    linkedin_url = Column(EncryptedText, nullable=True)
    background_info = Column(EncryptedText, nullable=True)
    background_generated = Column(Boolean, server_default=text("false"))
    auto_workflows_generated = Column(Boolean, server_default=text("false"))
    onboarding_completed = Column(Boolean, server_default=text("false"))
    onboarding_stage = Column(String(50), nullable=True)
    feature_action_items = Column(Boolean, server_default=text("true"))
    feature_email_drafts = Column(Boolean, server_default=text("true"))
    feature_meeting_prep = Column(Boolean, server_default=text("true"))
    feature_dev_console = Column(Boolean, server_default=text("false"))
    last_scheduler_run = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    skills = relationship("Skill", back_populates="user", lazy="selectin")


class Integration(Base):
    __tablename__ = "integrations"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), nullable=False)
    provider = Column(String(50), nullable=False)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text)
    bot_token = Column(Text)
    token_expiry = Column(DateTime(timezone=True))
    scopes = Column(ARRAY(String))
    extra_data = Column(JSONB)
    is_active = Column(Boolean, server_default=text("true"))
    error_count = Column(Integer, server_default=text("0"))
    last_error_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    connected_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"))


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), nullable=False)
    title = Column(String(500))
    # When True, the user manually renamed the title — auto-generation must
    # not overwrite it. Set by the rename endpoint, checked by _save_title_background.
    title_locked = Column(Boolean, nullable=False, server_default=text("false"))
    link_shared = Column(Boolean, server_default=text("false"))
    shared_with_emails = Column(ARRAY(String), server_default=text("'{}'"))
    # SHA256 hex digests (lowercased email) of recipients that have already
    # been Slack-DM'd that this chat was shared with them. Stored as
    # hashes (not raw emails) so we don't permanently persist plaintext
    # PII for a field that only needs equality-based dedupe. Append-only:
    # removing someone from shared_with_emails does NOT clear them here,
    # so re-adding later won't re-trigger a DM. See
    # services/share_notification_service.py for the hash helper.
    share_notified_email_hashes = Column(ARRAY(String), server_default=text("'{}'"))
    is_pinned = Column(Boolean, nullable=False, server_default=text("false"))
    # Auto-compaction: rolling summary of messages up to `summary_last_message_id`.
    # Messages newer than that id are loaded verbatim; older ones are represented by `summary`.
    summary = Column(EncryptedText, nullable=True)
    summary_last_message_id = Column(PGUUID(as_uuid=True), nullable=True)
    # Anthropic code_execution container continuity. Containers persist filesystem
    # state (files, pandas frames, etc.) across turns. We pass `container_id` back
    # on each request while it's unexpired; after expiry, Claude gets a fresh one.
    code_exec_container_id = Column(String(255), nullable=True)
    code_exec_container_expires_at = Column(DateTime(timezone=True), nullable=True)
    # UUID of the turn currently streaming for this conversation. Set when a
    # background producer starts, cleared when it finishes. Drives the
    # concurrent-send guard and the frontend resume path.
    active_turn_id = Column(PGUUID(as_uuid=True), nullable=True)
    # When this conversation was seeded from a workflow run's "Follow up"
    # action, these point back at the originating run + workflow so the chat
    # UI can show a "Continuing from workflow: ..." banner with a deep link.
    # Both denormalized for cheap reads (avoids a workflow_runs lookup on
    # every chat page load).
    workflow_run_id = Column(PGUUID(as_uuid=True), nullable=True)
    workflow_id = Column(PGUUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"))


class Message(Base):
    __tablename__ = "messages"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    conversation_id = Column(PGUUID(as_uuid=True), nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(EncryptedText)
    tool_calls = Column(EncryptedJSON)
    tool_results = Column(EncryptedJSON)
    model = Column(String(100))
    tokens_used = Column(Integer)
    thinking = Column(EncryptedText, nullable=True)  # Claude extended thinking content
    feedback = Column(String(10), nullable=True)  # "up", "down", or null
    feedback_text = Column(EncryptedText, nullable=True)  # optional user comment
    mentions = Column(EncryptedJSON, nullable=True)
    # Lifecycle state for assistant messages. User/tool messages stay "done".
    # "streaming" rows are produced in the background; their content is
    # authoritative only in the Redis stream until the producer finalizes them.
    status = Column(String(20), nullable=False, server_default=text("'done'"))
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))


class Skill(Base):
    __tablename__ = "skills"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    prompt = Column(Text, nullable=False)
    integrations_used = Column(ARRAY(String), server_default=text("'{}'"))
    memory_content = Column(EncryptedText, nullable=True)
    is_default = Column(Boolean, server_default=text("false"))
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(50), server_default=text("'idle'"))
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    user = relationship("User", back_populates="skills")


class CustomSlashCommand(Base):
    """User-defined slash command. Built-in commands (/eod, /research, etc.)
    live in code at services.slash_commands.BUILTIN_COMMANDS — this table
    only holds user overrides + custom commands. Built-ins are merged with
    rows here at parse/manifest time; a user row with the same slug as a
    built-in wins."""
    __tablename__ = "custom_slash_commands"
    # `(user_id, slug)` is the lookup key chat_service hits per turn — the
    # router's `scalar_one_or_none()` path assumes at most one row matches,
    # and the create endpoint relies on this constraint to surface a 409 on
    # duplicates. Mirrored in migrations/init.sql so create_all() and the
    # raw SQL migration agree.
    __table_args__ = (
        UniqueConstraint("user_id", "slug", name="uq_custom_slash_commands_user_slug"),
    )

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Slug is plaintext + queryable. chat_service does a single-row lookup
    # by `(user_id, slug)` on every chat send.
    slug = Column(String(64), nullable=False)
    # Author-supplied content. Encrypted because the prompt template can
    # encode workflows / customer names / project specifics — same risk
    # profile as Workflow.prompt.
    name = Column(EncryptedText, nullable=False)
    description = Column(EncryptedText, nullable=True)
    param_hint = Column(EncryptedText, nullable=True)
    prompt_template = Column(EncryptedText, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"))


class Workflow(Base):
    __tablename__ = "workflows"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(EncryptedText, nullable=False)
    description = Column(EncryptedText, nullable=True)
    prompt = Column(EncryptedText, nullable=False, server_default=text("''"))

    # When type
    when_type = Column(String(50), server_default=text("'schedule'"))  # "schedule" or "trigger"

    # Schedule fields
    schedule_start_date = Column(Date, nullable=True)
    schedule_interval = Column(Integer, server_default=text("1"))
    schedule_unit = Column(String(20), server_default=text("'day'"))  # "day" or "week"
    schedule_day_of_week = Column(String(10), nullable=True)  # "MO","TU","WE","TH","FR","SA","SU"
    # Optional weekday filter, applies to ANY unit. When non-null + non-empty,
    # the workflow only runs if today's weekday is in this list. Use cases:
    # weekdays-only (["MO","TU","WE","TH","FR"]), weekends (["SA","SU"]), or
    # arbitrary custom selection. Null/empty = no filter (legacy behavior).
    schedule_days_of_week = Column(JSONB, nullable=True)
    schedule_time = Column(String(10), server_default=text("'00:30'"))  # HH:MM

    # Trigger fields
    trigger_integration = Column(String(50), nullable=True)  # "google_calendar","gmail","slack"
    trigger_event = Column(String(50), nullable=True)  # "before_event","new_email","new_message"
    trigger_minutes_before = Column(Integer, nullable=True)
    trigger_filter = Column(JSONB, nullable=True)

    integrations_used = Column(ARRAY(String), server_default=text("'{}'"))
    status = Column(String(50), server_default=text("'inactive'"))  # "inactive" or "active"
    approval_mode = Column(String(50), server_default=text("'auto'"))  # "auto" or "manual"
    # Slack DM alert preference per run outcome. App-layer enum (no DB CHECK so
    # new modes don't need a migration): "none" | "failure" | "success" | "both".
    alert_on = Column(String(20), nullable=False, server_default=text("'failure'"))
    # LLM provider preference for this workflow's runs. 3-state (app-layer enum,
    # no DB CHECK so new providers don't need a migration):
    #   NULL       — AUTO: default provider (Kimi), and the executor may auto-pin
    #                to 'anthropic' after chronic Kimi->Anthropic escalation
    #                (see workflow_execution_service._maybe_pin_provider).
    #   'anthropic'— pinned: start on Anthropic 1M directly, skip the doomed Kimi
    #                attempt (set by auto-pin OR manually in the editor).
    #   'kimi'     — manual opt-out: always Kimi, never auto-pinned.
    preferred_provider = Column(String(20), nullable=True)
    active_until = Column(DateTime(timezone=True), nullable=True)  # NULL = forever; auto-deactivates when reached

    # Opt-in share-by-link. Defaults to False — only the owner sees/edits the
    # workflow until they explicitly toggle sharing (Share button flips it true).
    # Template + clone endpoints gate on this flag.
    link_shared = Column(Boolean, nullable=False, server_default=text("false"))

    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    user = relationship("User", backref="workflows")
    runs = relationship("WorkflowRun", back_populates="workflow", cascade="all, delete-orphan")


class WhatsAppLink(Base):
    """Verified binding between a WhatsApp phone number and a Gideon user.

    Created only after OTP verification in the in-app linking flow. Inbound
    WhatsApp messages resolve to a user via `phone_hash` (sha256 of the E.164
    number) — a deterministic lookup key kept plaintext, since Fernet ciphertext
    can't be matched. The raw number is stored encrypted for display/OTP-resend
    only. NEVER serve account data to a number without an active row here.
    """
    __tablename__ = "whatsapp_links"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # sha256(E.164) — unique lookup key for inbound resolution. Plaintext by
    # necessity (encrypted values aren't matchable); reveals nothing on its own.
    phone_hash = Column(String(64), nullable=False, unique=True)
    phone = Column(EncryptedText, nullable=False)  # raw E.164, encrypted (display/resend)
    is_active = Column(Boolean, server_default=text("true"), nullable=False)
    verified_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"))


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    workflow_id = Column(PGUUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    triggered_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    completed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(50), server_default=text("'running'"))  # running, pending_approval, approved, rejected, completed, failed
    # How the run was triggered. "scheduled" — workflow_scheduler_loop hit
    # the daily window. "manual" — user clicked Run Now in the FE / called
    # POST /workflows/{id}/runs. The `_was_run_today` check in the scheduler
    # only counts source='scheduled' rows, so a manual test run doesn't
    # consume the day's automatic run.
    source = Column(String(20), server_default=text("'manual'"), nullable=False)
    output = Column(EncryptedText, nullable=True)
    error = Column(EncryptedText, nullable=True)
    # Tangible things the model created during the run. Each entry:
    #   {kind, url, title, created_at, tool_call_id?}
    # Captured server-side from `mark_artifact` tool calls.
    artifacts = Column(EncryptedJSON, nullable=True)
    # Always-present markdown report. Populated from the model's final text
    # output even when no external artifact was created — this is what the
    # `/workflows/{id}/runs/{run_id}` page renders, and what the Slack DM
    # links to when no model-created artifact exists.
    report_markdown = Column(EncryptedText, nullable=True)
    # Full Anthropic-format message list captured at run completion: thinking
    # blocks, tool_use, tool_result, advisor blocks — everything Claude saw.
    # Used to seed a `Conversation` when the user clicks "Follow up" on the
    # run detail page so the chat resumes with full context (not just the
    # final report). Stored verbatim — workflow runs can be MBs encrypted.
    replay_messages = Column(EncryptedJSON, nullable=True)
    # Set when the user follows up on this run — links to the chat conv
    # spawned from the run. Idempotent: subsequent "Follow up" clicks return
    # the same conv. Nullable for runs that were never followed up.
    conversation_id = Column(PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)

    # Crash-resilient execution: per-round checkpoint so an interrupted run can
    # resume instead of re-running from scratch. `replay_messages` doubles as the
    # checkpoint message state (written each round, not only at completion).
    #   last_round       — next loop round to execute on resume (0-based)
    #   last_progress_at — heartbeat, written each round; drives stale-run reaping
    #   resume_count     — bounds resume attempts (poison-run guard)
    last_round = Column(Integer, nullable=True)
    last_progress_at = Column(DateTime(timezone=True), nullable=True)
    resume_count = Column(Integer, server_default=text("0"), nullable=False)
    # Sticky: set once a run escalates Kimi -> Anthropic 1M, so a resume starts
    # on Anthropic directly instead of re-overflowing Kimi.
    escalated_to_anthropic = Column(Boolean, server_default=text("false"), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    workflow = relationship("Workflow", back_populates="runs")


class Todo(Base):
    __tablename__ = "todos"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content = Column("text", EncryptedText, nullable=False)
    completed = Column(Boolean, server_default=text("false"))
    priority = Column(String(20), server_default=text("'none'"))  # none, low, medium, high
    source = Column(String(50), server_default=text("'manual'"))  # manual, suggestion
    source_detail = Column(Text, nullable=True)
    assigned_to_gideon = Column(Boolean, server_default=text("false"))
    assigned_conversation_id = Column(String, nullable=True)
    agent_status = Column(String, nullable=True)  # "working", "completed", "failed"
    parent_id = Column(PGUUID(as_uuid=True), ForeignKey("todos.id", ondelete="CASCADE"), nullable=True)
    snoozed_until = Column(DateTime(timezone=True), nullable=True)
    # Delegation (outbound -- I delegated to others)
    delegated_to = Column(EncryptedJSON, nullable=True)  # [{name, email}] array of delegatees
    slack_channel_id = Column(String, nullable=True)  # group DM channel for delegation
    slack_thread_ts = Column(String, nullable=True)  # thread ts in that channel
    # Delegation (inbound -- delegated to me)
    delegated_from_todo_id = Column(PGUUID(as_uuid=True), ForeignKey("todos.id", ondelete="SET NULL"), nullable=True)
    delegated_from_user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Recurring delegation reminders. Cadence is a free-form keyword (e.g.
    # "daily", "every_2_days", "weekly") — validated application-side via
    # `services.delegation_service.normalize_cadence`. No DB CHECK so new
    # cadences can ship without a schema migration.
    reminder_cadence = Column(String(20), nullable=True)
    next_reminder_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    user = relationship("User", backref="todos", foreign_keys=[user_id])
    delegated_from_user = relationship("User", foreign_keys=[delegated_from_user_id])
    subtasks = relationship(
        "Todo",
        backref=backref("parent", remote_side=[id]),
        foreign_keys=[parent_id],
        lazy="noload",
        cascade="all, delete-orphan",
    )
    updates = relationship("TodoUpdate", back_populates="todo", cascade="all, delete-orphan", lazy="noload")


class TodoSuggestion(Base):
    __tablename__ = "todo_suggestions"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content = Column("text", EncryptedText, nullable=False)
    detail = Column(EncryptedText, nullable=True)
    source_integration = Column(String(50), nullable=True)  # google, slack
    dismissed = Column(Boolean, server_default=text("false"))
    added_as_todo = Column(Boolean, server_default=text("false"))
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    user = relationship("User", backref="todo_suggestions")


class TodoUpdate(Base):
    __tablename__ = "todo_updates"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    todo_id = Column(PGUUID(as_uuid=True), ForeignKey("todos.id", ondelete="CASCADE"), nullable=False)
    author_name = Column(EncryptedText, nullable=False)
    author_email = Column(EncryptedText, nullable=True)
    content = Column(EncryptedText, nullable=False)
    source = Column(String(50), nullable=False)  # "slack", "nudge", "manual", "delegation"
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    todo = relationship("Todo", back_populates="updates")


class Memory(Base):
    __tablename__ = "memories"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content = Column(Text, nullable=False)
    category = Column(String(50), server_default=text("'general'"))  # general, preference, person, project, fact
    source = Column(String(50), server_default=text("'auto'"))  # auto, manual, tool
    source_conversation_id = Column(String(255), nullable=True)
    is_active = Column(Boolean, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    user = relationship("User", backref="memories")


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    source = Column(String(50), nullable=False)
    event_type = Column(String(100), nullable=False)
    title = Column(EncryptedText, nullable=False)
    summary = Column(EncryptedText)
    reference_id = Column(Text)
    extra_data = Column("metadata", EncryptedJSON)
    topic_id = Column(PGUUID(as_uuid=True), ForeignKey("activity_topics.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    user = relationship("User", backref="activity_logs")
    topic = relationship("ActivityTopic", backref="activities")


class ActivityTopic(Base):
    __tablename__ = "activity_topics"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    title = Column(EncryptedText, nullable=False)
    summary = Column(EncryptedText)
    last_activity_at = Column(DateTime(timezone=True))
    activity_count = Column(Integer, server_default=text("1"))
    mem0_memory_id = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    user = relationship("User", backref="activity_topics")


class TriggerConversation(Base):
    """Links external Slack threads / Gmail threads to internal Gideon conversations."""
    __tablename__ = "trigger_conversations"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    conversation_id = Column(PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)

    source = Column(String(20), nullable=False)  # 'slack' or 'gmail'
    source_thread_id = Column(Text, nullable=False)  # "channel:thread_ts" or gmail thread_id
    source_channel_id = Column(Text, nullable=True)  # Slack channel (for reply routing)
    source_message_ts = Column(Text, nullable=True)  # Slack thread_ts (for reply routing)
    source_email = Column(EncryptedText, nullable=True)  # sender email (to resolve user on follow-up)

    status = Column(String(20), server_default=text("'active'"))  # active, completed
    last_activity_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    user = relationship("User", backref="trigger_conversations")
    conversation = relationship("Conversation")


class MeetingPrep(Base):
    __tablename__ = "meeting_preps"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(String(500), nullable=False)  # Google Calendar event ID
    title = Column(EncryptedText, nullable=False)
    summary = Column(EncryptedText)  # one-liner AI summary
    prep_content = Column(EncryptedText)  # full markdown meeting prep
    attendees = Column(EncryptedJSON)  # cached attendees list
    event_start = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), server_default=text("'pending'"))  # pending, generating, completed, failed
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    user = relationship("User", backref="meeting_preps")


class EmailAutoDraft(Base):
    __tablename__ = "email_auto_drafts"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    email_id = Column(String(500), nullable=False)  # Gmail message ID
    thread_id = Column(String(500))
    sender_name = Column(EncryptedText)
    sender_email = Column(EncryptedText, nullable=False)
    sender_avatar_url = Column(Text)
    subject = Column(EncryptedText, nullable=False)
    email_body = Column(EncryptedText)  # Original email content (truncated)
    email_date = Column(DateTime(timezone=True))
    draft_body = Column(EncryptedText)  # AI-generated reply
    gmail_draft_id = Column(String(500))
    status = Column(String(20), server_default=text("'pending'"))  # pending, generating, ready, sent, skipped, failed
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    user = relationship("User", backref="email_auto_drafts")


class UsageLog(Base):
    __tablename__ = "usage_log"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    model = Column(String(100), nullable=False)
    use_case = Column(String(50), nullable=False)
    input_tokens = Column(Integer, server_default=text("0"))
    output_tokens = Column(Integer, server_default=text("0"))
    cache_read_tokens = Column(Integer, server_default=text("0"))
    cache_creation_tokens = Column(Integer, server_default=text("0"))
    web_search_requests = Column(Integer, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    user = relationship("User", backref="usage_logs")


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    conversation_id = Column(PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(500), nullable=False)
    mime_type = Column(String(100), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    extracted_text = Column(EncryptedText)  # None for images (use vision)
    is_image = Column(Boolean, server_default=text("false"))
    image_base64 = Column(EncryptedText)  # base64 for images (Claude vision)
    drive_file_id = Column(String(255))
    drive_url = Column(String(1000))
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    user = relationship("User", backref="uploaded_files")
    conversation = relationship("Conversation", backref="uploaded_files")


class Survey(Base):
    __tablename__ = "surveys"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    slug = Column(String(64), unique=True, nullable=False)
    title = Column(Text, nullable=False)
    intro_text = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, server_default=text("'draft'"))
    eligibility = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    questions = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # Cached AI analysis (themes / score distributions / usage correlation).
    # Generated on-demand by services/survey_analysis_service.py.
    analysis = Column(JSONB, nullable=True)
    analysis_generated_at = Column(DateTime(timezone=True), nullable=True)
    # Conversation Gideon uses to chat with the admin while building this survey.
    # One conversation per draft; created lazily on first builder open.
    builder_conversation_id = Column(PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)
    created_by = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    closed_at = Column(DateTime(timezone=True), nullable=True)


class SurveyResponse(Base):
    __tablename__ = "survey_responses"
    __table_args__ = (UniqueConstraint("survey_id", "user_id", name="uq_survey_responses_survey_user"),)

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    survey_id = Column(PGUUID(as_uuid=True), ForeignKey("surveys.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(20), nullable=False)
    answers = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    dismissed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))


