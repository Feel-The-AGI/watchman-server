-- Restrict billing, admin data and profile authority to the backend.
-- Apply through an authorized database connection after reviewing current grants.
-- The frontend uses Supabase Auth; profile/billing writes go through FastAPI.
BEGIN;

-- An own-row UPDATE policy also permits changing tier/role/auth_id. Revoke
-- direct writes instead of relying on the browser to omit protected columns.
DROP POLICY IF EXISTS users_update_own ON public.users;
REVOKE INSERT, UPDATE, DELETE ON public.users FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.users TO service_role;

DROP POLICY IF EXISTS subscriptions_insert_own ON public.subscriptions;
DROP POLICY IF EXISTS subscriptions_update_own ON public.subscriptions;
REVOKE INSERT, UPDATE, DELETE ON public.subscriptions FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.subscriptions TO service_role;

-- A policy name does not restrict its role. The previous WITH CHECK (true)
-- insert policy applied to PUBLIC and could allow fabricated payment records.
DROP POLICY IF EXISTS "Service role can insert payments" ON public.payments;
CREATE POLICY "Service role can insert payments" ON public.payments
    FOR INSERT TO service_role WITH CHECK (true);
REVOKE INSERT, UPDATE, DELETE ON public.payments FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.payments TO service_role;

DROP POLICY IF EXISTS "Service role can manage admin stats" ON public.admin_stats_cache;
CREATE POLICY "Service role can manage admin stats" ON public.admin_stats_cache
    FOR ALL TO service_role USING (true) WITH CHECK (true);
REVOKE ALL ON public.admin_stats_cache FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.admin_stats_cache TO service_role;

-- Public viewers must use the share-code API, which resolves a specific token.
-- A public SELECT policy exposes an enumerable list of every active share.
DROP POLICY IF EXISTS "Anyone can view active shares" ON public.calendar_shares;
REVOKE SELECT ON public.calendar_shares FROM PUBLIC, anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.calendar_shares TO service_role;

COMMIT;
