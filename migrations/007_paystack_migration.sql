-- Migration 007: Add Paystack columns (replacing Stripe)
-- Run this in Supabase SQL Editor

-- Add Paystack columns to users table
DO $$
BEGIN
    -- Add paystack_customer_code if not exists
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'paystack_customer_code'
    ) THEN
        ALTER TABLE users ADD COLUMN paystack_customer_code VARCHAR(255);
    END IF;

    -- Add paystack_subscription_code if not exists
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'paystack_subscription_code'
    ) THEN
        ALTER TABLE users ADD COLUMN paystack_subscription_code VARCHAR(255);
    END IF;
END $$;

-- Create index for Paystack lookups
CREATE INDEX IF NOT EXISTS idx_users_paystack_customer ON users(paystack_customer_code);

-- Add Paystack columns to payments table
DO $$
BEGIN
    -- Add paystack_reference if not exists
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'payments' AND column_name = 'paystack_reference'
    ) THEN
        ALTER TABLE payments ADD COLUMN paystack_reference VARCHAR(255);
    END IF;

    -- Add amount_local for GHS amount
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'payments' AND column_name = 'amount_local'
    ) THEN
        ALTER TABLE payments ADD COLUMN amount_local DECIMAL(10, 2);
    END IF;

    -- Add currency_local for actual charge currency (GHS)
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'payments' AND column_name = 'currency_local'
    ) THEN
        ALTER TABLE payments ADD COLUMN currency_local VARCHAR(3);
    END IF;

    -- Add exchange_rate used for conversion
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'payments' AND column_name = 'exchange_rate'
    ) THEN
        ALTER TABLE payments ADD COLUMN exchange_rate DECIMAL(10, 4);
    END IF;
END $$;

-- Create index for Paystack reference lookups
CREATE INDEX IF NOT EXISTS idx_payments_paystack_ref ON payments(paystack_reference);
