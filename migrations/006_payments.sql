-- Migration 006: Payments table for tracking subscription payments
-- Run this in Supabase SQL Editor

-- Create payments table
CREATE TABLE IF NOT EXISTS payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stripe_invoice_id VARCHAR(255),
    amount DECIMAL(10, 2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'usd',
    status VARCHAR(50) NOT NULL DEFAULT 'paid',
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create index for faster lookups
CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at DESC);

-- Enable RLS
ALTER TABLE payments ENABLE ROW LEVEL SECURITY;

-- Policy: Users can only see their own payments
CREATE POLICY "Users can view own payments"
    ON payments FOR SELECT
    USING (auth.uid()::text = (SELECT auth_id::text FROM users WHERE id = payments.user_id));

-- Policy: Only service role can insert payments (from webhook)
CREATE POLICY "Service role can insert payments"
    ON payments FOR INSERT
    WITH CHECK (true);

-- Add stripe_customer_id to users table if not exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'stripe_customer_id'
    ) THEN
        ALTER TABLE users ADD COLUMN stripe_customer_id VARCHAR(255);
    END IF;
END $$;

-- Add stripe_subscription_id to users table if not exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'stripe_subscription_id'
    ) THEN
        ALTER TABLE users ADD COLUMN stripe_subscription_id VARCHAR(255);
    END IF;
END $$;

-- Create index for stripe lookups
CREATE INDEX IF NOT EXISTS idx_users_stripe_customer ON users(stripe_customer_id);
