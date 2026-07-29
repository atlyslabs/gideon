-- Gideon — Complete database schema
-- Consolidated from migrations 001-017
--
-- NOTE: Many columns hold Fernet-encrypted ciphertext (see db/encrypted_types.py).
-- Encryption is applied at the application layer via SQLAlchemy TypeDecorators.
-- Columns marked "Fernet-encrypted" below store base64 tokens starting with "gAAAAA".
-- Previously JSONB columns are now TEXT because ciphertext is opaque to JSONB operators.

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ─────────────────────────────────────────────────────────────
-- Users (synced from the identity provider on first login)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_provider_id VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(255),
    persona_text TEXT,
    timezone VARCHAR(50) DEFAULT 'UTC',
    preferred_communication VARCHAR(50) DEFAULT 'gmail',
    city VARCHAR(100),
    role VARCHAR(100),
    department VARCHAR(100),
    linkedin_url VARCHAR(500),
    background_info TEXT,
    background_generated BOOLEAN DEFAULT false,
    auto_workflows_generated BOOLEAN DEFAULT false,
    onboarding_completed BOOLEAN DEFAULT false,
    onboarding_stage VARCHAR(50),
    feature_action_items BOOLEAN DEFAULT true,
    feature_email_drafts BOOLEAN DEFAULT true,
    feature_meeting_prep BOOLEAN DEFAULT true,
    feature_dev_console BOOLEAN DEFAULT false,
    last_scheduler_run TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- OAuth integrations
-- ─────────────────────────────────────────────────────────────
CREATE TABLE integrations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider VARCHAR(50) NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    bot_token TEXT,
    token_expiry TIMESTAMPTZ,
    scopes TEXT[],
    extra_data JSONB,
    is_active BOOLEAN DEFAULT true,
    error_count INT DEFAULT 0,
    last_error_at TIMESTAMPTZ,
    last_error TEXT,
    connected_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, provider)
);

CREATE INDEX idx_integrations_user ON integrations(user_id);

-- ─────────────────────────────────────────────────────────────
-- Chat conversations
-- ─────────────────────────────────────────────────────────────
CREATE TABLE conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(500),
    title_locked BOOLEAN NOT NULL DEFAULT false,
    link_shared BOOLEAN DEFAULT false,
    shared_with_emails TEXT[] DEFAULT '{}',
    -- SHA256 hex digests of (lowercased) recipient emails that have
    -- already been Slack-DM'd. Stored as hashes (not raw emails) so we
    -- don't permanently persist plaintext PII for what is only an
    -- equality-based dedupe field. Append-only: removing someone from
    -- shared_with_emails doesn't clear them from here, so re-adding
    -- doesn't re-trigger a DM. See services/share_notification_service.py.
    share_notified_email_hashes TEXT[] DEFAULT '{}',
    is_pinned BOOLEAN NOT NULL DEFAULT false,
    summary TEXT,
    summary_last_message_id UUID,
    code_exec_container_id VARCHAR(255),
    code_exec_container_expires_at TIMESTAMPTZ,
    active_turn_id UUID,  -- set while a background producer is running; cleared when it finishes
    -- When this conversation was seeded from a workflow run's "Follow up"
    -- action, points back at the originating workflow_runs.id (and workflow)
    -- so the chat UI can render a "Continuing from workflow: …" banner.
    workflow_run_id UUID,
    workflow_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_conversations_user ON conversations(user_id, is_pinned DESC, updated_at DESC);

-- ─────────────────────────────────────────────────────────────
-- Chat messages
-- ─────────────────────────────────────────────────────────────
CREATE TABLE messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL,
    content TEXT,          -- Fernet-encrypted via EncryptedText
    tool_calls TEXT,       -- Fernet-encrypted JSON via EncryptedJSON
    tool_results TEXT,     -- Fernet-encrypted JSON via EncryptedJSON
    model VARCHAR(100),
    tokens_used INTEGER,
    thinking TEXT,         -- Fernet-encrypted via EncryptedText
    feedback VARCHAR(10),
    feedback_text TEXT,    -- Fernet-encrypted via EncryptedText
    mentions TEXT,         -- Fernet-encrypted JSON via EncryptedJSON
    status VARCHAR(20) NOT NULL DEFAULT 'done'
        CHECK (status IN ('streaming', 'done', 'error', 'interrupted', 'aborted')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at);

ALTER TABLE conversations
    ADD CONSTRAINT fk_conversations_summary_last_message
    FOREIGN KEY (summary_last_message_id) REFERENCES messages(id) ON DELETE SET NULL;

-- ─────────────────────────────────────────────────────────────
-- Skills
-- ─────────────────────────────────────────────────────────────
CREATE TABLE skills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    prompt TEXT NOT NULL,
    integrations_used TEXT[] DEFAULT '{}',
    memory_content TEXT,
    is_default BOOLEAN DEFAULT false,
    last_run_at TIMESTAMPTZ,
    status VARCHAR(50) DEFAULT 'idle',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_skills_user ON skills(user_id);
CREATE INDEX idx_skills_user_default ON skills(user_id, is_default);

-- ─────────────────────────────────────────────────────────────
-- Custom slash commands (user-defined; built-ins live in code)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE custom_slash_commands (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- Slug stays plaintext + indexed: chat_service does a per-turn lookup
    -- on (user_id, slug). Encrypted columns can't satisfy equality.
    slug VARCHAR(64) NOT NULL,
    name TEXT NOT NULL,             -- EncryptedText
    description TEXT,                -- EncryptedText, nullable
    param_hint TEXT,                 -- EncryptedText, nullable
    prompt_template TEXT NOT NULL,   -- EncryptedText
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, slug)
);

CREATE INDEX idx_custom_slash_commands_user ON custom_slash_commands(user_id);

-- ─────────────────────────────────────────────────────────────
-- Workflows + runs
-- ─────────────────────────────────────────────────────────────
CREATE TABLE workflows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    prompt TEXT NOT NULL DEFAULT '',
    when_type VARCHAR(50) DEFAULT 'schedule',
    schedule_start_date DATE,
    schedule_interval INTEGER DEFAULT 1,
    schedule_unit VARCHAR(20) DEFAULT 'day',
    schedule_day_of_week VARCHAR(10),
    schedule_days_of_week JSONB,
    schedule_time VARCHAR(10) DEFAULT '00:30',
    trigger_integration VARCHAR(50),
    trigger_event VARCHAR(50),
    trigger_minutes_before INTEGER,
    trigger_filter JSONB,
    integrations_used TEXT[] DEFAULT '{}',
    status VARCHAR(50) DEFAULT 'inactive',
    approval_mode VARCHAR(50) DEFAULT 'auto',
    alert_on VARCHAR(20) NOT NULL DEFAULT 'failure',  -- 'none' | 'failure' | 'success' | 'both'
    preferred_provider VARCHAR(20),  -- NULL=auto (Kimi, may auto-pin) | 'anthropic' (pinned 1M) | 'kimi' (manual opt-out)
    active_until TIMESTAMPTZ,
    link_shared BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_workflows_user ON workflows(user_id);
CREATE INDEX idx_workflows_user_status ON workflows(user_id, status);

CREATE TABLE workflow_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id UUID NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    triggered_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status VARCHAR(50) DEFAULT 'running',
    -- 'scheduled' | 'manual'. _was_run_today() in scheduler.py only counts
    -- 'scheduled' rows so manual test runs don't burn the day's auto-run.
    source VARCHAR(20) NOT NULL DEFAULT 'manual',
    output TEXT,
    error TEXT,
    -- Tangible model-created outputs (Doc/Sheet/Notion/Linear/Slack message…)
    -- captured via the `mark_artifact` tool. Encrypted JSON list.
    artifacts TEXT,
    -- Always-present markdown report — rendered by the run-detail page and
    -- linked from the Slack DM when no model artifact exists. Encrypted.
    report_markdown TEXT,
    -- Full Anthropic-format message history captured at run completion.
    -- Used to seed a Conversation when the user clicks "Follow up" so chat
    -- resumes with full context (thinking + tool_use + tool_result blocks).
    -- Encrypted JSON; can be large for runs with many tool calls.
    replay_messages TEXT,
    -- Set when the user follows up — links to the spawned Conversation.
    -- Idempotent: subsequent "Follow up" clicks return the same conv.
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_workflow_runs_workflow ON workflow_runs(workflow_id);
CREATE INDEX idx_workflow_runs_user ON workflow_runs(user_id);
CREATE INDEX idx_workflow_runs_workflow_triggered ON workflow_runs(workflow_id, triggered_at DESC);

-- ─────────────────────────────────────────────────────────────
-- Todos + suggestions
-- ─────────────────────────────────────────────────────────────
CREATE TABLE todos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    completed BOOLEAN DEFAULT false,
    priority VARCHAR(20) DEFAULT 'none',
    source VARCHAR(50) DEFAULT 'manual',
    source_detail TEXT,
    assigned_to_gideon BOOLEAN DEFAULT false,
    assigned_conversation_id VARCHAR,
    agent_status VARCHAR,
    parent_id UUID REFERENCES todos(id) ON DELETE CASCADE,
    snoozed_until TIMESTAMPTZ,
    -- Delegation
    delegated_to TEXT,               -- Fernet-encrypted JSON: [{name, email}] via EncryptedJSON
    slack_channel_id TEXT,            -- group DM channel for delegation thread
    slack_thread_ts TEXT,             -- thread ts in that channel
    -- Delegation inbound (delegated to me)
    delegated_from_todo_id UUID REFERENCES todos(id) ON DELETE SET NULL,
    delegated_from_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    -- Recurring delegation reminders. Cadence is a free-form keyword (e.g.
    -- 'daily' / 'every_2_days' / 'weekly') validated by the application; we
    -- intentionally don't enforce it via DB CHECK so adding new cadences
    -- later doesn't require a schema migration.
    reminder_cadence VARCHAR(20),
    next_reminder_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_todos_user ON todos(user_id);
CREATE INDEX idx_todos_user_completed ON todos(user_id, completed);
CREATE INDEX idx_todos_parent ON todos(parent_id);
CREATE INDEX idx_todos_delegated_to_me ON todos(user_id) WHERE delegated_from_todo_id IS NOT NULL;
CREATE INDEX idx_todos_snoozed ON todos(user_id, snoozed_until) WHERE snoozed_until IS NOT NULL;
CREATE INDEX idx_todos_delegated ON todos(user_id) WHERE delegated_to IS NOT NULL AND completed = false;

-- Todo delegation timeline
CREATE TABLE todo_updates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    todo_id UUID NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
    author_name VARCHAR(255) NOT NULL,
    author_email VARCHAR(255),
    content TEXT NOT NULL,
    source VARCHAR(50) NOT NULL,      -- 'slack', 'nudge', 'manual', 'delegation'
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_todo_updates_todo ON todo_updates(todo_id);

CREATE TABLE todo_suggestions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    detail TEXT,
    source_integration VARCHAR(50),
    dismissed BOOLEAN DEFAULT false,
    added_as_todo BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_todo_suggestions_user ON todo_suggestions(user_id);
CREATE INDEX idx_todo_suggestions_user_active ON todo_suggestions(user_id, dismissed, added_as_todo);

-- ─────────────────────────────────────────────────────────────
-- Memories
-- ─────────────────────────────────────────────────────────────
CREATE TABLE memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    category VARCHAR(50) NOT NULL DEFAULT 'general',
    source VARCHAR(50) NOT NULL DEFAULT 'auto',
    source_conversation_id VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_memories_user_id ON memories(user_id);
CREATE INDEX idx_memories_user_active ON memories(user_id, is_active);

-- ─────────────────────────────────────────────────────────────
-- Activity log + topics
-- ─────────────────────────────────────────────────────────────
CREATE TABLE activity_topics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    title TEXT NOT NULL,
    summary TEXT,
    last_activity_at TIMESTAMPTZ,
    activity_count INT DEFAULT 1,
    mem0_memory_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_topics_user_time ON activity_topics(user_id, last_activity_at DESC);

CREATE TABLE activity_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    source VARCHAR(50) NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    reference_id TEXT,
    metadata TEXT,                   -- Fernet-encrypted JSON via EncryptedJSON (was JSONB before encryption migration)
    topic_id UUID REFERENCES activity_topics(id),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_activity_user_time ON activity_log(user_id, created_at DESC);
CREATE INDEX idx_activity_reference ON activity_log(user_id, source, reference_id);
CREATE INDEX idx_activity_topic ON activity_log(topic_id);
CREATE UNIQUE INDEX idx_activity_unique_ref ON activity_log(user_id, source, reference_id) WHERE reference_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────
-- Trigger conversations (Slack @Gideon / the Gideon mailbox)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE trigger_conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    source VARCHAR(20) NOT NULL,           -- 'slack' or 'gmail'
    source_thread_id TEXT NOT NULL,         -- "channel:thread_ts" or gmail thread_id
    source_channel_id TEXT,                -- Slack channel (for reply routing)
    source_message_ts TEXT,                -- Slack thread_ts (for reply routing)
    source_email TEXT,                     -- sender email (for Gmail user resolution)
    status VARCHAR(20) DEFAULT 'active',   -- active, completed
    last_activity_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_trigger_conv_source_thread ON trigger_conversations(user_id, source, source_thread_id);
CREATE INDEX idx_trigger_conv_user ON trigger_conversations(user_id);
CREATE INDEX idx_trigger_conv_conversation ON trigger_conversations(conversation_id);

-- ─────────────────────────────────────────────────────────────
-- Meeting prep (AI-generated meeting preparation notes)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE meeting_preps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id VARCHAR(500) NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    prep_content TEXT,
    attendees TEXT,                  -- Fernet-encrypted JSON via EncryptedJSON
    event_start TIMESTAMPTZ NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_meeting_preps_user_event ON meeting_preps(user_id, event_id);
CREATE INDEX idx_meeting_preps_user_start ON meeting_preps(user_id, event_start DESC);

-- ─────────────────────────────────────────────────────────────
-- Email auto-drafts (AI-generated reply suggestions)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE email_auto_drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email_id VARCHAR(500) NOT NULL,
    thread_id VARCHAR(500),
    sender_name TEXT,
    sender_email TEXT NOT NULL,
    sender_avatar_url TEXT,
    subject TEXT NOT NULL,
    email_body TEXT,
    email_date TIMESTAMPTZ,
    draft_body TEXT,
    gmail_draft_id VARCHAR(500),
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_email_auto_drafts_user_email ON email_auto_drafts(user_id, email_id);
CREATE INDEX idx_email_auto_drafts_user_status ON email_auto_drafts(user_id, status);

-- ─────────────────────────────────────────────────────────────
-- Usage tracking
-- ─────────────────────────────────────────────────────────────
CREATE TABLE usage_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model VARCHAR(100) NOT NULL,
    use_case VARCHAR(50) NOT NULL,
    input_tokens INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    cache_read_tokens INT DEFAULT 0,
    cache_creation_tokens INT DEFAULT 0,
    web_search_requests INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_usage_user_time ON usage_log(user_id, created_at DESC);
CREATE INDEX idx_usage_user_case ON usage_log(user_id, use_case);

-- ─────────────────────────────────────────────────────────────
-- Uploaded files (per-conversation file attachments)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE uploaded_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    filename VARCHAR(500) NOT NULL,
    mime_type VARCHAR(100) NOT NULL,
    size_bytes INT NOT NULL,
    extracted_text TEXT,
    is_image BOOLEAN DEFAULT false,
    image_base64 TEXT,
    drive_file_id VARCHAR(255),
    drive_url VARCHAR(1000),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_uploaded_files_conversation ON uploaded_files(conversation_id);
CREATE INDEX idx_uploaded_files_user ON uploaded_files(user_id, created_at DESC);

-- ─────────────────────────────────────────────────────────────
-- Idempotent additions for existing databases
-- ─────────────────────────────────────────────────────────────
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN NOT NULL DEFAULT false;
-- Mirrors idx_conversations_user above so fresh installs don't get a second
-- copy; this line exists for already-deployed DBs that predate is_pinned.
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, is_pinned DESC, updated_at DESC);

ALTER TABLE messages ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'done';
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'messages_status_check'
    ) THEN
        ALTER TABLE messages
            ADD CONSTRAINT messages_status_check
            CHECK (status IN ('streaming', 'done', 'error', 'interrupted', 'aborted'));
    END IF;
END $$;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS active_turn_id UUID;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS title_locked BOOLEAN NOT NULL DEFAULT false;

-- Recurring delegation reminders. Application-layer enforces the cadence
-- enum so the DB doesn't need a CHECK migration for new cadences.
ALTER TABLE todos ADD COLUMN IF NOT EXISTS reminder_cadence VARCHAR(20);
ALTER TABLE todos ADD COLUMN IF NOT EXISTS next_reminder_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_todos_due_reminders
    ON todos (next_reminder_at)
    WHERE next_reminder_at IS NOT NULL AND completed = false;

-- Workflow run artifacts + canonical report. Both columns are encrypted at
-- the application layer (TEXT to fit Fernet ciphertext). See WorkflowRun in
-- db/models.py for usage.
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS artifacts TEXT;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS report_markdown TEXT;

-- "Follow up in chat" support. Captures the full Anthropic-format message
-- list at run completion and lazily links a Conversation when the user
-- clicks the follow-up button on the run-detail page.
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS replay_messages TEXT;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS conversation_id UUID
    REFERENCES conversations(id) ON DELETE SET NULL;

-- Crash-resilient execution (2026-06): per-round checkpointing so an interrupted
-- run (deploy / pod recycle / OOM) can be resumed instead of re-run from scratch.
--   last_round       — next round to execute on resume (0-based loop index)
--   last_progress_at — heartbeat written each round; drives stale-run reaping
--                      (safe under rolling-deploy pod overlap: a live run keeps
--                       heartbeating, so it's never falsely reaped)
--   resume_count     — bounds resume attempts so a poison run can't crash-loop
-- The checkpoint message state reuses the existing `replay_messages` column,
-- written per-round (not only at completion).
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS last_round INTEGER;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS last_progress_at TIMESTAMPTZ;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS resume_count INTEGER NOT NULL DEFAULT 0;
-- Sticky Kimi->Anthropic escalation: once a run outgrows Kimi's window and
-- switches to Anthropic's 1M, persist it so a resume starts on Anthropic
-- directly instead of re-hitting the same overflow on Kimi.
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS escalated_to_anthropic BOOLEAN NOT NULL DEFAULT false;
-- Reaper/resumer scan `running`/`interrupted` rows by recency — index it.
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status_progress
    ON workflow_runs(status, last_progress_at);
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS workflow_run_id UUID;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS workflow_id UUID;
-- SHA256 hex digests of recipients that have already been Slack-DM'd a
-- "share opened" notification. Hashed (not raw emails) for PII hygiene.
-- Append-only dedupe (no re-notify on remove + re-add).
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS share_notified_email_hashes TEXT[] DEFAULT '{}';

-- WhatsApp entry point (2026-06): verified phone <-> user binding. Inbound
-- resolution is by phone_hash (sha256 of E.164) since encrypted values can't be
-- matched; the raw number is stored encrypted (TEXT) for display only.
CREATE TABLE IF NOT EXISTS whatsapp_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    phone_hash VARCHAR(64) NOT NULL UNIQUE,
    phone TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    verified_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_whatsapp_links_user ON whatsapp_links(user_id);

-- Idempotent creation of custom_slash_commands (added April 2026 with the
-- user-defined slash commands feature).
CREATE TABLE IF NOT EXISTS custom_slash_commands (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    slug VARCHAR(64) NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    param_hint TEXT,
    prompt_template TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_custom_slash_commands_user ON custom_slash_commands(user_id);

-- ─────────────────────────────────────────────────────────────
-- Surveys (in-app blocking surveys, scoped via eligibility)
-- ─────────────────────────────────────────────────────────────
-- `eligibility` is a JSONB blob:
--   {
--     "departments": ["Engineering", "Support"],
--     "role_keywords": ["Manager", "Lead"],          -- case-insensitive substring match
--     "emails": ["person@example.com"]               -- explicit allow-list (additive)
--   }
-- A user passes if their email is in `emails` OR
-- (departments empty OR user.department in departments) AND
-- (role_keywords empty OR any keyword is a substring of user.role).
--
-- `questions` is a JSONB list:
--   [{"id": "q1", "type": "likert_1_5"|"text"|"single_select"|"multi_select",
--     "prompt": "...", "options": ["a","b"], "required": true}]
CREATE TABLE IF NOT EXISTS surveys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug VARCHAR(64) UNIQUE NOT NULL,
    title TEXT NOT NULL,
    intro_text TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'closed')),
    eligibility JSONB NOT NULL DEFAULT '{}'::jsonb,
    questions JSONB NOT NULL DEFAULT '[]'::jsonb,
    analysis JSONB,
    analysis_generated_at TIMESTAMPTZ,
    builder_conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_surveys_status ON surveys(status) WHERE status = 'active';
-- Looked up on every chat turn in services/chat_service.py to detect the
-- builder context. Partial index keeps it tiny (most surveys never get a
-- builder convo, and the column is NULL until first builder open).
CREATE INDEX IF NOT EXISTS idx_surveys_builder_conv ON surveys(builder_conversation_id)
    WHERE builder_conversation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS survey_responses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    survey_id UUID NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status VARCHAR(20) NOT NULL CHECK (status IN ('submitted', 'dismissed')),
    answers JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {question_id: value}
    submitted_at TIMESTAMPTZ,
    dismissed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    -- Name MUST match the `constraint=` arg in pg_insert(...).on_conflict_do_update
    -- in routers/surveys_router.py — Postgres-auto-generated names won't match.
    CONSTRAINT uq_survey_responses_survey_user UNIQUE (survey_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_survey_responses_user ON survey_responses(user_id);
CREATE INDEX IF NOT EXISTS idx_survey_responses_survey ON survey_responses(survey_id);

