-- Give each verified Pro payment a bounded access period.
-- Apply after migration 009, before deploying the entitlement code.
-- This adds columns and backfills expiry. It does not delete user data.
--
-- Paid Pro users whose latest payment is already older than 30 days receive
-- 7 days of access from the time this migration runs, not an immediate cutoff.
-- Pro users with no paid payment (complimentary grants) keep a null expiry,
-- which the API treats as non-expiring. Admin accounts are not changed.
BEGIN;

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS pro_expires_at TIMESTAMPTZ;

ALTER TABLE public.payments
    ADD COLUMN IF NOT EXISTS access_until TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_users_pro_expires_at ON public.users (pro_expires_at);

-- One server-created Paystack reference can grant a period only once.
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_paystack_reference_unique
    ON public.payments (paystack_reference)
    WHERE paystack_reference IS NOT NULL;

UPDATE public.users AS users
SET pro_expires_at = GREATEST(
    payments.last_paid + INTERVAL '30 days',
    NOW() + INTERVAL '7 days'
)
FROM (
    SELECT user_id, MAX(created_at) AS last_paid
    FROM public.payments
    WHERE status = 'paid'
    GROUP BY user_id
) AS payments
WHERE users.id = payments.user_id
  AND users.tier = 'pro'
  AND users.pro_expires_at IS NULL;

COMMIT;
