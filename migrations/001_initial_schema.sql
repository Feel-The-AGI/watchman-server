-- Watchman Database Schema
-- A deterministic life-state simulator with approval-gated mutations

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================
-- ENUMS
-- ============================================

CREATE TYPE user_tier AS ENUM ('free', 'pro', 'admin');
CREATE TYPE user_role AS ENUM ('user', 'admin');
CREATE TYPE work_type AS ENUM ('work_day', 'work_night', 'off');
CREATE TYPE commitment_type AS ENUM ('work', 'education', 'personal', 'leave', 'study', 'sleep');
CREATE TYPE commitment_status AS ENUM ('active', 'queued', 'completed', 'paused');
CREATE TYPE mutation_status AS ENUM ('proposed', 'approved', 'rejected', 'expired');
CREATE TYPE constraint_mode AS ENUM ('binary', 'weighted');
CREATE TYPE subscription_status AS ENUM ('active', 'cancelled', 'expired', 'past_due');

-- ============================================
-- USERS TABLE
-- ============================================

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    auth_id UUID UNIQUE NOT NULL, -- Supabase auth.users.id
    email VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    timezone VARCHAR(100) DEFAULT 'UTC',
    tier user_tier DEFAULT 'free',
    role user_role DEFAULT 'user',
    onboarding_completed BOOLEAN DEFAULT FALSE,
    settings JSONB DEFAULT '{
        "constraint_mode": "binary",
        "weighted_mode_enabled": false,
        "max_concurrent_commitments": 2,
        "notifications_email": true,
        "notifications_whatsapp": false,
        "theme": "dark"
    }'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- SUBSCRIPTIONS TABLE
-- ============================================

CREATE TABLE subscriptions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tier user_tier NOT NULL DEFAULT 'free',
    status subscription_status DEFAULT 'active',
    stripe_customer_id VARCHAR(255),
    stripe_subscription_id VARCHAR(255),
    current_period_start TIMESTAMPTZ,
    current_period_end TIMESTAMPTZ,
    cancel_at_period_end BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- CYCLES TABLE (Work Rotation Patterns)
-- ============================================

CREATE TABLE cycles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL DEFAULT 'Default Rotation',
    is_active BOOLEAN DEFAULT TRUE,
    -- Pattern defines the rotation structure
    -- Example: [{"label": "work_day", "duration": 10}, {"label": "work_night", "duration": 5}, {"label": "off", "duration": 10}]
    pattern JSONB NOT NULL,
    -- Total cycle length in days (sum of all durations)
    cycle_length INTEGER NOT NULL,
    -- Anchor defines where in the cycle a specific date falls
    anchor_date DATE NOT NULL,
    anchor_cycle_day INTEGER NOT NULL, -- 1-indexed (e.g., Day 4 of cycle)
    -- Metadata
    crew VARCHAR(50), -- Optional crew identifier (e.g., "Crew C")
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT valid_anchor_cycle_day CHECK (anchor_cycle_day >= 1 AND anchor_cycle_day <= cycle_length)
);

-- ============================================
-- CONSTRAINTS TABLE (Binary Rules)
-- ============================================

CREATE TABLE constraints (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    -- Constraint rule definition
    -- Examples:
    -- {"type": "no_study_on", "values": ["work_night"]}
    -- {"type": "max_concurrent", "scope": "education", "value": 2}
    -- {"type": "required_gap", "after": "work_night", "hours": 8}
    rule JSONB NOT NULL,
    -- Priority for weighted mode (higher = more important)
    weight INTEGER DEFAULT 100,
    -- Whether this is a system constraint or user-defined
    is_system BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- COMMITMENTS TABLE (Education, Personal Goals, etc.)
-- ============================================

CREATE TABLE commitments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    type commitment_type NOT NULL,
    status commitment_status DEFAULT 'active',
    -- Priority tier (1 = highest priority)
    priority INTEGER DEFAULT 1,
    -- Scheduling constraints
    -- Example: {"study_on": ["off", "work_day_evening"], "exclude": ["work_night"], "frequency": "weekly", "duration_hours": 3}
    constraints_json JSONB DEFAULT '{}'::jsonb,
    -- Time bounds
    start_date DATE,
    end_date DATE,
    -- Recurrence pattern (if applicable)
    -- Example: {"type": "weekly", "days": [1, 3], "time": "18:00"}
    recurrence JSONB,
    -- Tracking
    total_sessions INTEGER,
    completed_sessions INTEGER DEFAULT 0,
    -- Source of the commitment
    source VARCHAR(50) DEFAULT 'manual', -- manual, parsed, imported
    source_text TEXT, -- Original text if parsed from LLM
    -- Metadata
    color VARCHAR(7), -- Hex color for UI
    icon VARCHAR(50),
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- LEAVE BLOCKS TABLE
-- ============================================

CREATE TABLE leave_blocks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL DEFAULT 'Leave',
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    -- Effects on work constraints
    effects JSONB DEFAULT '{
        "work": "suspended",
        "available_time": "increased"
    }'::jsonb,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT valid_date_range CHECK (end_date >= start_date)
);

-- ============================================
-- CALENDAR DAYS TABLE (Generated Day-by-Day State)
-- ============================================

CREATE TABLE calendar_days (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    -- Cycle information
    cycle_id UUID REFERENCES cycles(id) ON DELETE SET NULL,
    cycle_day INTEGER, -- Which day of the cycle this is
    work_type work_type NOT NULL,
    -- Aggregated state for the day
    -- Contains all commitments, their time slots, and computed properties
    state_json JSONB DEFAULT '{
        "commitments": [],
        "available_hours": 0,
        "used_hours": 0,
        "is_overloaded": false,
        "is_leave": false,
        "tags": []
    }'::jsonb,
    -- Quick access fields derived from state_json
    is_work_day BOOLEAN GENERATED ALWAYS AS (work_type IN ('work_day', 'work_night')) STORED,
    is_off_day BOOLEAN GENERATED ALWAYS AS (work_type = 'off') STORED,
    is_night_shift BOOLEAN GENERATED ALWAYS AS (work_type = 'work_night') STORED,
    -- Version for conflict detection
    version INTEGER DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, date)
);

-- ============================================
-- MUTATIONS LOG TABLE (Undo/Redo Support)
-- ============================================

CREATE TABLE mutations_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status mutation_status DEFAULT 'proposed',
    -- What was the intent of this mutation
    intent VARCHAR(100) NOT NULL,
    -- The scope of dates affected
    scope_start DATE,
    scope_end DATE,
    -- The actual changes proposed/applied
    -- Contains before/after state diffs
    proposed_diff JSONB NOT NULL,
    -- If this mutation was generated by alternatives engine
    is_alternative BOOLEAN DEFAULT FALSE,
    parent_mutation_id UUID REFERENCES mutations_log(id),
    -- Human-readable explanation of changes
    explanation TEXT,
    -- Reasons for failure/rejection if applicable
    failure_reasons JSONB,
    -- Alternative proposals generated if this failed
    alternatives JSONB,
    -- Constraint violations detected
    violations JSONB,
    -- For undo/redo chain
    previous_state_hash VARCHAR(64),
    new_state_hash VARCHAR(64),
    -- When the mutation was processed
    proposed_at TIMESTAMPTZ DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    applied_at TIMESTAMPTZ,
    -- Who/what triggered this mutation
    triggered_by VARCHAR(50) DEFAULT 'user', -- user, system, llm
    source_text TEXT, -- Original input text if from LLM parsing
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- CALENDAR SNAPSHOTS TABLE (For Undo/Redo)
-- ============================================

CREATE TABLE calendar_snapshots (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mutation_id UUID REFERENCES mutations_log(id) ON DELETE SET NULL,
    state_hash VARCHAR(64) NOT NULL,
    -- Full calendar state at this point
    snapshot JSONB NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- INDEXES
-- ============================================

-- Users
CREATE INDEX idx_users_auth_id ON users(auth_id);
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_tier ON users(tier);

-- Subscriptions
CREATE INDEX idx_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX idx_subscriptions_status ON subscriptions(status);

-- Cycles
CREATE INDEX idx_cycles_user_id ON cycles(user_id);
CREATE INDEX idx_cycles_active ON cycles(user_id, is_active) WHERE is_active = TRUE;

-- Constraints
CREATE INDEX idx_constraints_user_id ON constraints(user_id);
CREATE INDEX idx_constraints_active ON constraints(user_id, is_active) WHERE is_active = TRUE;

-- Commitments
CREATE INDEX idx_commitments_user_id ON commitments(user_id);
CREATE INDEX idx_commitments_status ON commitments(user_id, status);
CREATE INDEX idx_commitments_type ON commitments(user_id, type);
CREATE INDEX idx_commitments_dates ON commitments(user_id, start_date, end_date);

-- Leave blocks
CREATE INDEX idx_leave_blocks_user_id ON leave_blocks(user_id);
CREATE INDEX idx_leave_blocks_dates ON leave_blocks(user_id, start_date, end_date);

-- Calendar days
CREATE INDEX idx_calendar_days_user_date ON calendar_days(user_id, date);
CREATE INDEX idx_calendar_days_date_range ON calendar_days(user_id, date) 
    INCLUDE (work_type, cycle_day);
CREATE INDEX idx_calendar_days_work_type ON calendar_days(user_id, work_type);

-- Mutations log
CREATE INDEX idx_mutations_user_id ON mutations_log(user_id);
CREATE INDEX idx_mutations_status ON mutations_log(user_id, status);
CREATE INDEX idx_mutations_proposed_at ON mutations_log(user_id, proposed_at DESC);

-- Snapshots
CREATE INDEX idx_snapshots_user_id ON calendar_snapshots(user_id);
CREATE INDEX idx_snapshots_hash ON calendar_snapshots(state_hash);

-- ============================================
-- ROW LEVEL SECURITY
-- ============================================

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cycles ENABLE ROW LEVEL SECURITY;
ALTER TABLE constraints ENABLE ROW LEVEL SECURITY;
ALTER TABLE commitments ENABLE ROW LEVEL SECURITY;
ALTER TABLE leave_blocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE calendar_days ENABLE ROW LEVEL SECURITY;
ALTER TABLE mutations_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE calendar_snapshots ENABLE ROW LEVEL SECURITY;

-- Users policies
CREATE POLICY users_select_own ON users FOR SELECT USING (auth.uid() = auth_id);
CREATE POLICY users_update_own ON users FOR UPDATE USING (auth.uid() = auth_id);

-- Subscriptions policies
CREATE POLICY subscriptions_select_own ON subscriptions FOR SELECT 
    USING (user_id IN (SELECT id FROM users WHERE auth_id = auth.uid()));
CREATE POLICY subscriptions_insert_own ON subscriptions FOR INSERT 
    WITH CHECK (user_id IN (SELECT id FROM users WHERE auth_id = auth.uid()));
CREATE POLICY subscriptions_update_own ON subscriptions FOR UPDATE 
    USING (user_id IN (SELECT id FROM users WHERE auth_id = auth.uid()));

-- Cycles policies
CREATE POLICY cycles_all_own ON cycles FOR ALL 
    USING (user_id IN (SELECT id FROM users WHERE auth_id = auth.uid()));

-- Constraints policies
CREATE POLICY constraints_all_own ON constraints FOR ALL 
    USING (user_id IN (SELECT id FROM users WHERE auth_id = auth.uid()));

-- Commitments policies
CREATE POLICY commitments_all_own ON commitments FOR ALL 
    USING (user_id IN (SELECT id FROM users WHERE auth_id = auth.uid()));

-- Leave blocks policies
CREATE POLICY leave_blocks_all_own ON leave_blocks FOR ALL 
    USING (user_id IN (SELECT id FROM users WHERE auth_id = auth.uid()));

-- Calendar days policies
CREATE POLICY calendar_days_all_own ON calendar_days FOR ALL 
    USING (user_id IN (SELECT id FROM users WHERE auth_id = auth.uid()));

-- Mutations log policies
CREATE POLICY mutations_all_own ON mutations_log FOR ALL 
    USING (user_id IN (SELECT id FROM users WHERE auth_id = auth.uid()));

-- Snapshots policies
CREATE POLICY snapshots_all_own ON calendar_snapshots FOR ALL 
    USING (user_id IN (SELECT id FROM users WHERE auth_id = auth.uid()));

-- ============================================
-- FUNCTIONS
-- ============================================

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply updated_at triggers
CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_subscriptions_updated_at BEFORE UPDATE ON subscriptions 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_cycles_updated_at BEFORE UPDATE ON cycles 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_constraints_updated_at BEFORE UPDATE ON constraints 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_commitments_updated_at BEFORE UPDATE ON commitments 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_leave_blocks_updated_at BEFORE UPDATE ON leave_blocks 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_calendar_days_updated_at BEFORE UPDATE ON calendar_days 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Function to create user profile after auth signup
CREATE OR REPLACE FUNCTION handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.users (auth_id, email, name)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(NEW.raw_user_meta_data->>'name', split_part(NEW.email, '@', 1))
    );
    RETURN NEW;
END;
$$ language 'plpgsql' SECURITY DEFINER;

-- Trigger for new user signup
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION handle_new_user();

-- ============================================
-- SEED DEFAULT CONSTRAINTS (System-level)
-- ============================================

-- These will be created per-user during onboarding, but here's the template
COMMENT ON TABLE constraints IS 'Default system constraints to create for each user:
- No study on night shifts
- Maximum 2 concurrent education commitments
- Work is immutable (cannot be removed or modified)
- Required 8-hour gap after night shift before study';
