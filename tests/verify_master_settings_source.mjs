// Apply migration 011 twice on an isolated database. Never connects to production.
// Run: PGLITE_PACKAGE_JSON=/path/to/package.json node tests/verify_master_settings_source.mjs
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';

const require = createRequire(process.env.PGLITE_PACKAGE_JSON || import.meta.url);
const { PGlite } = require('@electric-sql/pglite');
const db = new PGlite();

await db.exec(`
  CREATE TABLE cycles (
    id uuid PRIMARY KEY,
    user_id uuid,
    name text,
    pattern jsonb,
    anchor_date date,
    anchor_cycle_day int,
    cycle_length int,
    is_active boolean,
    updated_at timestamptz
  );
  CREATE TABLE constraints (
    id uuid PRIMARY KEY,
    user_id uuid,
    name text,
    description text,
    rule jsonb,
    is_active boolean,
    is_system boolean,
    created_at timestamptz
  );
  CREATE TABLE leave_blocks (
    id uuid PRIMARY KEY,
    user_id uuid,
    name text,
    start_date date,
    end_date date,
    notes text
  );
  CREATE TABLE master_settings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid UNIQUE,
    settings jsonb NOT NULL DEFAULT '{}'::jsonb,
    version int NOT NULL DEFAULT 1
  );
  INSERT INTO cycles VALUES
    ('10000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'Old',
     '[{"label":"work_night","duration":5}]'::jsonb, '2026-01-01', 1, 5, true, NOW()),
    ('10000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000002', 'Ignored',
     '[{"label":"off","duration":1}]'::jsonb, '2020-01-01', 1, 1, true, NOW());
  INSERT INTO constraints VALUES
    ('20000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001',
     'No study on night shifts', 'Study is not allowed during night shift days',
     '{"type":"no_activity_on","activity":"study","work_types":["work_night"]}'::jsonb,
     true, true, NOW());
  INSERT INTO leave_blocks VALUES
    ('30000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001',
     'Leave', '2026-12-01', '2026-12-05', NULL);
  INSERT INTO master_settings (user_id, settings) VALUES
    ('00000000-0000-0000-0000-000000000001', '{"cycle":null,"constraints":[],"leave_blocks":[],"commitments":[]}'),
    ('00000000-0000-0000-0000-000000000002', '{"cycle":{"id":"chat","pattern":[{"label":"work_day","duration":2}]},"constraints":[{"id":"kept"}],"leave_blocks":[{"id":"kept-leave"}]}');
`);

const migration = await readFile(new URL('../migrations/011_master_settings_source.sql', import.meta.url), 'utf8');
await db.exec(migration);
await db.exec(migration);

const rows = await db.query(`SELECT user_id::text, settings FROM master_settings ORDER BY user_id`);
const empty = rows.rows[0].settings;
const filled = rows.rows[1].settings;
assert.equal(empty.cycle.id, '10000000-0000-0000-0000-000000000001');
assert.equal(empty.constraints[0].rule.type, 'no_activity_on');
assert.equal(empty.leave_blocks[0].name, 'Leave');
assert.equal(filled.cycle.id, 'chat');
assert.deepEqual(filled.constraints, [{ id: 'kept' }]);
assert.equal(filled.leave_blocks[0].id, 'kept-leave');
assert.equal((await db.query('SELECT count(*)::int AS n FROM cycles')).rows[0].n, 2);

await db.close();
console.log('Master Settings source migration preserved existing documents and filled only empty sections.');
