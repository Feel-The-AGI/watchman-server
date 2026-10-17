// Run from repo root with: node tests/verify_billing_authority.mjs
// Requires @electric-sql/pglite in the caller's Node resolution path.
// Uses an isolated PostgreSQL database; never connects to production.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
const require = createRequire(process.env.PGLITE_PACKAGE_JSON || import.meta.url);
const { PGlite } = require('@electric-sql/pglite');

const db = new PGlite();
await db.exec(`
  CREATE ROLE anon;
  CREATE ROLE authenticated;
  CREATE ROLE service_role BYPASSRLS;
  CREATE SCHEMA auth;
  CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS
    $$ SELECT nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
  GRANT USAGE ON SCHEMA public, auth TO anon, authenticated, service_role;
  CREATE TABLE users(id uuid PRIMARY KEY, auth_id uuid, tier text, role text);
  CREATE TABLE subscriptions(id uuid, user_id uuid, tier text);
  CREATE TABLE payments(id uuid, user_id uuid, amount numeric);
  CREATE TABLE admin_stats_cache(id uuid, stat_value jsonb);
  CREATE TABLE calendar_shares(id uuid, user_id uuid, share_code text, is_active boolean);
  GRANT ALL ON ALL TABLES IN SCHEMA public TO anon, authenticated, service_role;
  ALTER TABLE users ENABLE ROW LEVEL SECURITY;
  ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
  ALTER TABLE payments ENABLE ROW LEVEL SECURITY;
  ALTER TABLE admin_stats_cache ENABLE ROW LEVEL SECURITY;
  ALTER TABLE calendar_shares ENABLE ROW LEVEL SECURITY;
  CREATE POLICY users_select_own ON users FOR SELECT USING(auth.uid() = auth_id);
  CREATE POLICY users_update_own ON users FOR UPDATE USING(auth.uid() = auth_id);
  CREATE POLICY subscriptions_select_own ON subscriptions FOR SELECT
    USING(user_id IN (SELECT id FROM users WHERE auth_id = auth.uid()));
  CREATE POLICY subscriptions_insert_own ON subscriptions FOR INSERT WITH CHECK(true);
  CREATE POLICY subscriptions_update_own ON subscriptions FOR UPDATE USING(true);
  CREATE POLICY "Service role can insert payments" ON payments FOR INSERT WITH CHECK(true);
  CREATE POLICY "Users can view own payments" ON payments FOR SELECT
    USING(user_id IN (SELECT id FROM users WHERE auth_id = auth.uid()));
  CREATE POLICY "Service role can manage admin stats" ON admin_stats_cache
    FOR ALL USING(true) WITH CHECK(true);
  CREATE POLICY "Anyone can view active shares" ON calendar_shares
    FOR SELECT USING(is_active);
  CREATE POLICY "Users can manage own shares" ON calendar_shares FOR ALL
    USING(auth.uid() = (SELECT auth_id FROM users WHERE id=calendar_shares.user_id));
  INSERT INTO users VALUES
    ('00000000-0000-0000-0000-000000000001','10000000-0000-0000-0000-000000000001','free','user'),
    ('00000000-0000-0000-0000-000000000002','10000000-0000-0000-0000-000000000002','pro','user');
  INSERT INTO calendar_shares VALUES
    ('20000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002','private-code',true),
    ('20000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000001','own-code',true);
  SELECT set_config('request.jwt.claim.sub','10000000-0000-0000-0000-000000000001',false);
`);

// Confirm this fixture reproduces the original own-profile escalation.
await db.exec(`SET ROLE authenticated; UPDATE users SET tier='admin' WHERE auth_id=auth.uid(); RESET ROLE;`);
assert.equal((await db.query(`SELECT tier FROM users WHERE id='00000000-0000-0000-0000-000000000001'`)).rows[0].tier, 'admin');
await db.exec(`UPDATE users SET tier='free';`);
const migration = await readFile(new URL('../migrations/009_billing_authority.sql', import.meta.url), 'utf8');
await db.exec(migration);
await db.exec(migration); // Safe to rerun.

for (const role of ['anon', 'authenticated']) {
  await db.exec(`SET ROLE ${role}`);
  for (const sql of [
    `UPDATE users SET tier='admin' WHERE auth_id=auth.uid()`,
    `UPDATE users SET role='admin' WHERE auth_id=auth.uid()`,
    `INSERT INTO subscriptions VALUES (gen_random_uuid(),'00000000-0000-0000-0000-000000000001','pro')`,
    `INSERT INTO payments VALUES (gen_random_uuid(),'00000000-0000-0000-0000-000000000001',12)`,
    `SELECT * FROM admin_stats_cache`,
    `UPDATE admin_stats_cache SET stat_value='{}'`,
  ]) await assert.rejects(db.exec(sql), /permission denied/);
  if (role === 'anon') await assert.rejects(db.exec('SELECT * FROM calendar_shares'), /permission denied/);
  else {
    const shares = (await db.query('SELECT share_code FROM calendar_shares')).rows;
    assert.deepEqual(shares, [{ share_code: 'own-code' }]);
  }
  await db.exec('RESET ROLE');
}
await db.exec('SET ROLE authenticated');
assert.equal((await db.query('SELECT * FROM users')).rows.length, 1);
await db.exec('RESET ROLE; SET ROLE service_role');
await db.exec(`UPDATE users SET tier='pro' WHERE id='00000000-0000-0000-0000-000000000001'`);
await db.exec(`INSERT INTO payments VALUES (gen_random_uuid(),'00000000-0000-0000-0000-000000000001',12)`);
assert.equal((await db.query('SELECT * FROM calendar_shares')).rows.length, 2);
await db.exec('RESET ROLE');
await db.close();
console.log('Billing authority migration passed PostgreSQL role, isolation and repeat-application checks.');
