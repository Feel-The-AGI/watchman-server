// Apply migration 010 to an isolated database. Never connects to production.
// Run: PGLITE_PACKAGE_JSON=/path/to/package.json node tests/verify_pro_period.mjs
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';

const require = createRequire(process.env.PGLITE_PACKAGE_JSON || import.meta.url);
const { PGlite } = require('@electric-sql/pglite');
const db = new PGlite();

await db.exec(`
  CREATE TABLE users (
    id uuid PRIMARY KEY,
    tier text,
    pro_expires_at timestamptz
  );
  CREATE TABLE payments (
    id uuid PRIMARY KEY,
    user_id uuid,
    status text,
    created_at timestamptz,
    paystack_reference text,
    access_until timestamptz
  );
  INSERT INTO users VALUES
    ('00000000-0000-0000-0000-000000000001', 'pro', NULL),
    ('00000000-0000-0000-0000-000000000002', 'pro', NULL),
    ('00000000-0000-0000-0000-000000000003', 'pro', NULL),
    ('00000000-0000-0000-0000-000000000004', 'admin', NULL),
    ('00000000-0000-0000-0000-000000000005', 'free', NULL);
  INSERT INTO payments VALUES
    ('10000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'paid', NOW() - INTERVAL '400 days', 'old', NULL),
    ('10000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000002', 'paid', NOW() - INTERVAL '1 day', 'recent', NULL),
    ('10000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000004', 'paid', NOW() - INTERVAL '400 days', 'admin-pay', NULL),
    ('10000000-0000-0000-0000-000000000004', '00000000-0000-0000-0000-000000000005', 'paid', NOW() - INTERVAL '1 day', 'free-pay', NULL);
`);

const migration = await readFile(new URL('../migrations/010_pro_access_period.sql', import.meta.url), 'utf8');
await db.exec(migration);
const first = await db.query(`SELECT id::text, pro_expires_at FROM users ORDER BY id`);
await db.exec(migration);
const second = await db.query(`SELECT id::text, pro_expires_at FROM users ORDER BY id`);
assert.deepEqual(second.rows, first.rows);

const rows = Object.fromEntries(second.rows.map((row) => [row.id, row.pro_expires_at]));
const now = new Date();
const oldExpiry = new Date(rows['00000000-0000-0000-0000-000000000001']);
const recentExpiry = new Date(rows['00000000-0000-0000-0000-000000000002']);
assert.ok(oldExpiry.getTime() > now.getTime() + 6 * 86400000);
assert.ok(oldExpiry.getTime() < now.getTime() + 8 * 86400000);
assert.ok(recentExpiry.getTime() > now.getTime() + 28 * 86400000);
assert.ok(recentExpiry.getTime() < now.getTime() + 30 * 86400000);
assert.equal(rows['00000000-0000-0000-0000-000000000003'], null);
assert.equal(rows['00000000-0000-0000-0000-000000000004'], null);
assert.equal(rows['00000000-0000-0000-0000-000000000005'], null);

await db.close();
console.log('Pro access period migration passed backfill, grace, and repeat-application checks.');
