# Database authority repair

Migration 009 closes browser database permissions that bypass backend authorization:

- The own-profile update policy permits changing `tier`, `role`, and `auth_id`.
  The admin API treats `tier = admin` as authorization.
- Subscription rows can be inserted and updated directly by their owners.
- The payment insert policy is named for the service role but does not specify
  `TO service_role`; its unconditional check applies to all granted roles.
- The admin statistics policy likewise applies to all roles.
- Public reads of all active calendar shares expose share tokens as a list.

These are findings from the checked-in schema, not a claim that the production
database currently has the same grants or that exploitation has occurred.

The migration removes direct profile/billing writes for browser roles, restricts
the two service policies to their intended role, and removes public share-list
access. Existing user-owned reads and backend service operations are preserved.
It changes privileges and policies without deleting or rewriting user data.

## Validation

`tests/verify_billing_authority.mjs` reproduces the original profile escalation
in an isolated PostgreSQL instance, applies the migration twice, and verifies:

- anonymous and authenticated roles cannot modify profile authority or write
  subscriptions/payments;
- those roles cannot read or modify admin statistics;
- a user can still read their own profile and share, with other shares hidden;
- the service role can still upgrade a user, record a payment, and resolve shares.

Run with Node and `@electric-sql/pglite` installed in the module resolution path.
For an isolated dependency directory, set `PGLITE_PACKAGE_JSON` to its absolute
`package.json` path. This test does not connect to Supabase or production.

## Release requirements

Inspect current production policies and grants, then apply migration 009 using
the database owner. Confirm the same role checks through Supabase REST with
disposable accounts. The current browser uses Supabase Auth and the FastAPI
profile/billing APIs; these operations must continue to work after the migration.

Live database access is not available in this working session. The migration
has not been applied to production. Merging the PR alone does not run SQL.

The separate server security and proposal PRs remain necessary. Other known
launch blockers include subscription renewal/expiry, payment idempotency,
constraint fidelity, and authenticated browser validation.
