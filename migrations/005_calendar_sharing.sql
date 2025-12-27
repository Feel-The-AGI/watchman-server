-- Migration: 005_calendar_sharing
-- Description: Add calendar sharing feature for Pro users
-- Date: 2025-12-27

-- Create calendar_shares table
CREATE TABLE IF NOT EXISTS calendar_shares (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    share_code VARCHAR(32) UNIQUE NOT NULL,
    name VARCHAR(255) DEFAULT 'My Shared Calendar',
    show_commitments BOOLEAN DEFAULT FALSE,
    show_work_types BOOLEAN DEFAULT TRUE,
    is_active BOOLEAN DEFAULT TRUE,
    view_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create index for share_code lookups (public access)
CREATE INDEX IF NOT EXISTS idx_calendar_shares_code ON calendar_shares(share_code) WHERE is_active = TRUE;

-- Create index for user lookups
CREATE INDEX IF NOT EXISTS idx_calendar_shares_user ON calendar_shares(user_id);

-- Enable RLS
ALTER TABLE calendar_shares ENABLE ROW LEVEL SECURITY;

-- Policy: Users can manage their own shares
CREATE POLICY "Users can manage own shares" ON calendar_shares
    FOR ALL
    USING (auth.uid() = (SELECT auth_id::uuid FROM users WHERE id = calendar_shares.user_id));

-- Policy: Anyone can read active shares (for public view)
CREATE POLICY "Anyone can view active shares" ON calendar_shares
    FOR SELECT
    USING (is_active = TRUE);

-- Grant service role full access
GRANT ALL ON calendar_shares TO service_role;

-- Add updated_at trigger
CREATE TRIGGER update_calendar_shares_updated_at
    BEFORE UPDATE ON calendar_shares
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
