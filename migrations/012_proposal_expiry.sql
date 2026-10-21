-- Proposal expiry.
-- Migration 002 creates proposals.expires_at only when the table is new.
-- CREATE TABLE IF NOT EXISTS does not add the column to a table that already
-- exists, and PostgREST then rejects inserts with PGRST204.
-- Safe to run more than once. Does not rewrite existing rows.

ALTER TABLE public.proposals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_proposals_user_status_expires
    ON public.proposals (user_id, status, expires_at);
