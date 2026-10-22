-- Record which fixed-price plan a checkout charged.
-- Apply in the Supabase SQL editor. Deploying the API does not run this.
-- Checkout still starts if this column is missing: the plan id is also
-- written into payments.description as plan=<id>.
--
-- Legacy rows stay null. They keep verifying against amount_local.

ALTER TABLE public.payments
    ADD COLUMN IF NOT EXISTS plan TEXT;

COMMENT ON COLUMN public.payments.plan IS
    'Checkout plan id (monthly, yearly, founding_yearly). Null on legacy FX charges.';
