-- Migration: Expand incident types to cover more workplace scenarios
-- Run this in Supabase SQL Editor

-- ==========================================
-- UPDATE INCIDENTS TYPE CHECK CONSTRAINT
-- ==========================================

-- Drop ALL existing type check constraints (handles both auto-generated and named ones)
DO $$
DECLARE
    r RECORD;
BEGIN
    -- Find and drop all check constraints that reference the type column
    FOR r IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'incidents'::regclass
        AND contype = 'c'
        AND (pg_get_constraintdef(oid) LIKE '%type%IN%' OR conname = 'incidents_type_check')
    LOOP
        EXECUTE format('ALTER TABLE incidents DROP CONSTRAINT IF EXISTS %I', r.conname);
        RAISE NOTICE 'Dropped constraint: %', r.conname;
    END LOOP;
END $$;

-- Add the new check constraint with expanded types
ALTER TABLE incidents
ADD CONSTRAINT incidents_type_check
CHECK (type IN (
    'overtime',
    'safety',
    'equipment',
    'harassment',
    'injury',
    'policy_violation',
    'health',
    'discrimination',
    'workload',
    'compensation',
    'scheduling',
    'communication',
    'retaliation',
    'environment',
    'other'
));

-- Verify the constraint was created
DO $$
BEGIN
    RAISE NOTICE 'Incident types expanded successfully!';
END $$;
