-- Cap founding yearly at 100 successful seats, and remember who has the locked price.
-- Apply in the Supabase SQL editor. Deploying the API does not run this.
--
-- The trigger serializes claims. The 101st insert raises founding_cap.
-- The API still grants Pro for that paid charge and logs an error.
-- It does not void the payment.

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS founding_member BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS public.founding_claims (
    user_id UUID PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    payment_reference TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION public.limit_founding_claims()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    taken integer;
BEGIN
    -- Serialize claims without locking the table the INSERT already holds.
    PERFORM pg_advisory_xact_lock(81420100);
    SELECT COUNT(*) INTO taken FROM public.founding_claims;
    IF taken >= 100 THEN
        RAISE EXCEPTION 'founding_cap'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS founding_claims_cap ON public.founding_claims;
CREATE TRIGGER founding_claims_cap
    BEFORE INSERT ON public.founding_claims
    FOR EACH ROW
    EXECUTE FUNCTION public.limit_founding_claims();

ALTER TABLE public.founding_claims ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.founding_claims FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.founding_claims TO service_role;
