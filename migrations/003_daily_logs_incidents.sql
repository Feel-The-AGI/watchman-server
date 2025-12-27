-- Migration: Add daily_logs and incidents tables
-- Run this in Supabase SQL Editor

-- ==========================================
-- DAILY LOGS TABLE
-- ==========================================
CREATE TABLE IF NOT EXISTS daily_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    actual_hours FLOAT,
    overtime_hours FLOAT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    -- Ensure one log per user per date (for hours tracking)
    UNIQUE(user_id, date)
);

-- Index for faster queries
CREATE INDEX IF NOT EXISTS idx_daily_logs_user_date ON daily_logs(user_id, date);
CREATE INDEX IF NOT EXISTS idx_daily_logs_date ON daily_logs(date);

-- Enable RLS
ALTER TABLE daily_logs ENABLE ROW LEVEL SECURITY;

-- RLS Policies for daily_logs
CREATE POLICY "Users can view own daily logs" ON daily_logs
    FOR SELECT USING (auth.uid()::text = user_id::text);

CREATE POLICY "Users can create own daily logs" ON daily_logs
    FOR INSERT WITH CHECK (auth.uid()::text = user_id::text);

CREATE POLICY "Users can update own daily logs" ON daily_logs
    FOR UPDATE USING (auth.uid()::text = user_id::text);

CREATE POLICY "Users can delete own daily logs" ON daily_logs
    FOR DELETE USING (auth.uid()::text = user_id::text);

-- Service role bypass for admin operations
CREATE POLICY "Service role can manage all daily logs" ON daily_logs
    FOR ALL USING (auth.role() = 'service_role');


-- ==========================================
-- INCIDENTS TABLE
-- ==========================================
CREATE TABLE IF NOT EXISTS incidents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    type VARCHAR(50) NOT NULL CHECK (type IN ('overtime', 'safety', 'equipment', 'harassment', 'injury', 'policy_violation', 'other')),
    severity VARCHAR(20) NOT NULL DEFAULT 'medium' CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    title VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    reported_to VARCHAR(255),
    witnesses TEXT,
    outcome TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for faster queries
CREATE INDEX IF NOT EXISTS idx_incidents_user_date ON incidents(user_id, date);
CREATE INDEX IF NOT EXISTS idx_incidents_date ON incidents(date);
CREATE INDEX IF NOT EXISTS idx_incidents_type ON incidents(type);
CREATE INDEX IF NOT EXISTS idx_incidents_severity ON incidents(severity);

-- Enable RLS
ALTER TABLE incidents ENABLE ROW LEVEL SECURITY;

-- RLS Policies for incidents
CREATE POLICY "Users can view own incidents" ON incidents
    FOR SELECT USING (auth.uid()::text = user_id::text);

CREATE POLICY "Users can create own incidents" ON incidents
    FOR INSERT WITH CHECK (auth.uid()::text = user_id::text);

CREATE POLICY "Users can update own incidents" ON incidents
    FOR UPDATE USING (auth.uid()::text = user_id::text);

CREATE POLICY "Users can delete own incidents" ON incidents
    FOR DELETE USING (auth.uid()::text = user_id::text);

-- Service role bypass for admin operations
CREATE POLICY "Service role can manage all incidents" ON incidents
    FOR ALL USING (auth.role() = 'service_role');


-- ==========================================
-- UPDATED_AT TRIGGERS
-- ==========================================

-- Function to auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Trigger for daily_logs
DROP TRIGGER IF EXISTS update_daily_logs_updated_at ON daily_logs;
CREATE TRIGGER update_daily_logs_updated_at
    BEFORE UPDATE ON daily_logs
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Trigger for incidents
DROP TRIGGER IF EXISTS update_incidents_updated_at ON incidents;
CREATE TRIGGER update_incidents_updated_at
    BEFORE UPDATE ON incidents
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();


-- ==========================================
-- GRANT PERMISSIONS
-- ==========================================
GRANT ALL ON daily_logs TO authenticated;
GRANT ALL ON daily_logs TO service_role;
GRANT ALL ON incidents TO authenticated;
GRANT ALL ON incidents TO service_role;
