-- Stripe billing storage. Paystack columns and the payments routes are unchanged.
-- Run this in the Supabase SQL editor as the database owner.
-- Deploying the API does not apply it. Until it is applied, /api/billing/*
-- returns 503 Payments not configured.
--
-- stripe_founding_seats counts active and past founding subscriptions.
-- One row per person. Deleting the user sets user_id null and keeps the seat,
-- so the 100 cap does not reopen. The 101st new person raises founding_cap.
-- Safe to run more than once.

BEGIN;

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_users_stripe_customer ON public.users (stripe_customer_id);

ALTER TABLE public.subscriptions
    ADD COLUMN IF NOT EXISTS plan TEXT,
    ADD COLUMN IF NOT EXISTS stripe_status TEXT;

COMMENT ON COLUMN public.subscriptions.plan IS
    'Stripe plan: monthly, yearly, or founding. Null on Paystack-only rows.';

CREATE TABLE IF NOT EXISTS public.stripe_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.stripe_founding_seats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES public.users(id) ON DELETE SET NULL,
    stripe_subscription_id TEXT,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_stripe_founding_seats_user
    ON public.stripe_founding_seats (user_id)
    WHERE user_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_stripe_founding_seats_subscription
    ON public.stripe_founding_seats (stripe_subscription_id)
    WHERE stripe_subscription_id IS NOT NULL;

CREATE OR REPLACE FUNCTION public.limit_stripe_founding_seats()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    taken integer;
BEGIN
    PERFORM pg_advisory_xact_lock(81420136);
    IF NEW.user_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.stripe_founding_seats WHERE user_id = NEW.user_id
    ) THEN
        RETURN NEW;
    END IF;
    SELECT COUNT(*) INTO taken FROM public.stripe_founding_seats;
    IF taken >= 100 THEN
        RAISE EXCEPTION 'founding_cap'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS stripe_founding_seats_cap ON public.stripe_founding_seats;
CREATE TRIGGER stripe_founding_seats_cap
    BEFORE INSERT ON public.stripe_founding_seats
    FOR EACH ROW
    EXECUTE FUNCTION public.limit_stripe_founding_seats();

ALTER TABLE public.stripe_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stripe_founding_seats ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.stripe_events FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.stripe_founding_seats FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.stripe_events TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.stripe_founding_seats TO service_role;

COMMIT;
