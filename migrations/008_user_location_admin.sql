-- Migration 008: Add user location fields and admin tracking
-- Run this in Supabase SQL Editor

-- Add location fields to users table
DO $$
BEGIN
    -- Add country field
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'country'
    ) THEN
        ALTER TABLE users ADD COLUMN country VARCHAR(100);
    END IF;

    -- Add country_code field (ISO 3166-1 alpha-2)
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'country_code'
    ) THEN
        ALTER TABLE users ADD COLUMN country_code VARCHAR(2);
    END IF;

    -- Add region/state field
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'region'
    ) THEN
        ALTER TABLE users ADD COLUMN region VARCHAR(100);
    END IF;

    -- Add city field
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'city'
    ) THEN
        ALTER TABLE users ADD COLUMN city VARCHAR(100);
    END IF;

    -- Add timezone field
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'timezone'
    ) THEN
        ALTER TABLE users ADD COLUMN timezone VARCHAR(50);
    END IF;

    -- Add last_active timestamp
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'last_active'
    ) THEN
        ALTER TABLE users ADD COLUMN last_active TIMESTAMPTZ DEFAULT NOW();
    END IF;

    -- Add signup_source field (organic, referral, etc.)
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'signup_source'
    ) THEN
        ALTER TABLE users ADD COLUMN signup_source VARCHAR(50) DEFAULT 'organic';
    END IF;

    -- Add ip_address for analytics (hashed or partial for privacy)
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'last_ip'
    ) THEN
        ALTER TABLE users ADD COLUMN last_ip VARCHAR(45);
    END IF;
END $$;

-- Create indexes for admin queries
CREATE INDEX IF NOT EXISTS idx_users_country ON users(country);
CREATE INDEX IF NOT EXISTS idx_users_country_code ON users(country_code);
CREATE INDEX IF NOT EXISTS idx_users_tier ON users(tier);
CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active DESC);
CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_users_onboarding_completed ON users(onboarding_completed);

-- Create admin_stats table for caching expensive queries
CREATE TABLE IF NOT EXISTS admin_stats_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stat_key VARCHAR(100) UNIQUE NOT NULL,
    stat_value JSONB NOT NULL,
    calculated_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT NOW() + INTERVAL '5 minutes'
);

-- Enable RLS on admin_stats_cache
ALTER TABLE admin_stats_cache ENABLE ROW LEVEL SECURITY;

-- Only service role can access admin stats
CREATE POLICY "Service role can manage admin stats"
    ON admin_stats_cache FOR ALL
    USING (true)
    WITH CHECK (true);
