-- Migration: 002_overhaul_architecture
-- Description: Add Master Settings, Command Log, and Chat Messages for conversation-first architecture
-- This migration implements Phase 0 of the Watchman overhaul

-- ============================================================================
-- MASTER SETTINGS TABLE
-- Single source of truth for all user parameters
-- ============================================================================
CREATE TABLE IF NOT EXISTS master_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- The complete settings document as JSONB
    -- Structure: { cycle, work, constraints, commitments, leave_blocks, preferences }
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    
    -- Version for optimistic locking
    version INTEGER NOT NULL DEFAULT 1,
    
    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Only one master settings per user
    CONSTRAINT unique_user_master_settings UNIQUE (user_id)
);

-- Index for fast user lookup
CREATE INDEX IF NOT EXISTS idx_master_settings_user_id ON master_settings(user_id);

-- ============================================================================
-- COMMAND LOG TABLE (Undo Stack)
-- Every executed command is logged with before/after state
-- ============================================================================
CREATE TYPE command_status AS ENUM ('applied', 'undone', 'redone');

CREATE TABLE IF NOT EXISTS command_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- The command that was executed
    action VARCHAR(50) NOT NULL,  -- e.g., 'update_cycle', 'add_commitment'
    payload JSONB NOT NULL,       -- The command payload
    
    -- State snapshots for undo/redo
    before_state JSONB,           -- State before this command
    after_state JSONB,            -- State after this command
    
    -- Status tracking
    status command_status NOT NULL DEFAULT 'applied',
    
    -- Source tracking
    source VARCHAR(20) NOT NULL DEFAULT 'chat',  -- 'chat', 'ui', 'api'
    message_id UUID,              -- Link to chat message that triggered this
    
    -- Human-readable explanation
    explanation TEXT,
    
    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for user command history
CREATE INDEX IF NOT EXISTS idx_command_log_user_id ON command_log(user_id);
CREATE INDEX IF NOT EXISTS idx_command_log_created_at ON command_log(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_command_log_message_id ON command_log(message_id);

-- ============================================================================
-- CHAT MESSAGES TABLE
-- Conversation history between user and agent
-- ============================================================================
CREATE TYPE message_role AS ENUM ('user', 'assistant', 'system');

CREATE TABLE IF NOT EXISTS chat_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- Message content
    role message_role NOT NULL,
    content TEXT NOT NULL,
    
    -- Link to command if this message triggered one
    command_id UUID REFERENCES command_log(id) ON DELETE SET NULL,
    
    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb,  -- tokens_used, model, etc.
    
    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for user chat history
CREATE INDEX IF NOT EXISTS idx_chat_messages_user_id ON chat_messages(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_created_at ON chat_messages(user_id, created_at DESC);

-- ============================================================================
-- PROPOSALS TABLE (Updated)
-- Pending proposals awaiting user approval
-- ============================================================================
CREATE TYPE proposal_status AS ENUM ('pending', 'approved', 'rejected', 'expired');

CREATE TABLE IF NOT EXISTS proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- The proposed command
    command JSONB NOT NULL,  -- { action, payload, explanation }
    
    -- Validation result from Constraint Engine
    validation JSONB NOT NULL DEFAULT '{}'::jsonb,  -- { valid, violations, warnings, alternatives }
    
    -- Status
    status proposal_status NOT NULL DEFAULT 'pending',
    
    -- Related chat message
    message_id UUID REFERENCES chat_messages(id) ON DELETE SET NULL,
    
    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ  -- Auto-expire old proposals
);

-- Index for pending proposals
CREATE INDEX IF NOT EXISTS idx_proposals_user_pending ON proposals(user_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_proposals_created_at ON proposals(user_id, created_at DESC);

-- ============================================================================
-- ROW LEVEL SECURITY
-- ============================================================================

-- Master Settings RLS
ALTER TABLE master_settings ENABLE ROW LEVEL SECURITY;

CREATE POLICY master_settings_select ON master_settings
    FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY master_settings_insert ON master_settings
    FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY master_settings_update ON master_settings
    FOR UPDATE USING (auth.uid() = user_id);

CREATE POLICY master_settings_delete ON master_settings
    FOR DELETE USING (auth.uid() = user_id);

-- Command Log RLS
ALTER TABLE command_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY command_log_select ON command_log
    FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY command_log_insert ON command_log
    FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY command_log_update ON command_log
    FOR UPDATE USING (auth.uid() = user_id);

-- Chat Messages RLS
ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;

CREATE POLICY chat_messages_select ON chat_messages
    FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY chat_messages_insert ON chat_messages
    FOR INSERT WITH CHECK (auth.uid() = user_id);

-- Proposals RLS
ALTER TABLE proposals ENABLE ROW LEVEL SECURITY;

CREATE POLICY proposals_select ON proposals
    FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY proposals_insert ON proposals
    FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY proposals_update ON proposals
    FOR UPDATE USING (auth.uid() = user_id);

-- ============================================================================
-- HELPER FUNCTIONS
-- ============================================================================

-- Function to auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Trigger for master_settings
CREATE TRIGGER update_master_settings_updated_at
    BEFORE UPDATE ON master_settings
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- DEFAULT MASTER SETTINGS TEMPLATE
-- ============================================================================
COMMENT ON TABLE master_settings IS 'Default settings structure:
{
  "cycle": {
    "id": "uuid",
    "name": "My Rotation",
    "pattern": [
      {"type": "day_shift", "days": 5, "label": "Day"},
      {"type": "night_shift", "days": 5, "label": "Night"},
      {"type": "off", "days": 5, "label": "Off"}
    ],
    "anchor": {"date": "2026-01-01", "cycle_day": 1}
  },
  "work": {
    "shift_hours": 12,
    "shift_start": "06:00",
    "break_minutes": 60
  },
  "constraints": [],
  "commitments": [],
  "leave_blocks": [],
  "preferences": {
    "timezone": "UTC",
    "week_starts_on": "monday",
    "theme": "dark"
  }
}';
