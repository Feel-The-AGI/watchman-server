# Deploy Watchman API

The previous Render service (`watchman-api-dnm0.onrender.com`) is gone. This is a new host. Do not point a deploy at the production Supabase database until migrations 009 and 010 have been reviewed and applied by someone with database-owner access.

Merge order before the first production deploy:

1. `fix/security-user-isolation`
2. `fix/chat-history-and-approval`
3. `fix/database-billing-authority`
4. `cursor/entitlements-expiry-ea99`
5. `cursor/test-suite-triage-ea99`
6. `cursor/deploy-readiness-ea99` (this file and `docs/LAUNCH_DECISIONS.md`)
7. `cursor/master-settings-calendar-ea99` (Master Settings calendar source and the night-shift rule)

## 1. Secrets

Set these on the host. Names match `.env.example`.

| Variable | Where to get it |
| --- | --- |
| `SUPABASE_URL` | Supabase → Project Settings → API → Project URL |
| `SUPABASE_ANON_KEY` | Same page → anon public key |
| `SUPABASE_SERVICE_KEY` | Same page → service_role key. Server only. |
| `SUPABASE_JWT_SECRET` | Project Settings → API → JWT Secret |
| `ANTHROPIC_API_KEY` | Anthropic Console → API keys |
| `ANTHROPIC_MODEL` | Optional. Defaults to `claude-haiku-5-5` |
| `STRIPE_SECRET_KEY` | Stripe → Developers → API keys → Secret key (`sk_live_…`, or `sk_test_…` for test mode). |
| `STRIPE_WEBHOOK_SECRET` | Stripe → Developers → Webhooks → your endpoint → Signing secret (`whsec_…`). |
| `PRO_PRICE_USD` / `PRO_ANNUAL_PRICE_USD` | Optional. Default 12 and 96. The Stripe product and prices are created on the first checkout. |
| `APP_URL` | Optional. Defaults to `https://www.trywatchman.app`; used for checkout return links. |
| `RESEND_API_KEY` | Resend dashboard |
| `EMAIL_FROM` | `Watchman <notifications@trywatchman.app>` once that domain is verified in Resend |
| `CRON_SECRET` | A new long random string. Send it as the `X-Cron-Secret` header. |
| `CORS_ORIGINS` | The frontend origins, comma-separated, no spaces required but spaces are trimmed |
| `APP_ENV` | `production` |
| `DEBUG` | `false` |

Payments are Stripe subscriptions. Without `STRIPE_SECRET_KEY` checkout returns 503 and nothing is charged. Without `STRIPE_WEBHOOK_SECRET` the webhook returns 503. Paystack keys and `EXCHANGE_RATE_API_KEY` are no longer read and can be deleted from the dashboard.

The frontend on Vercel needs `NEXT_PUBLIC_API_URL` set to this service's public URL. That value lives in `watchman-client`, not here.

## 2. Database

Apply the SQL files in `migrations/` in filename order, in the Supabase SQL editor, as the database owner. Merging this repository does not run SQL.

Stop after reviewing each statement. None of these statements were run against production from the change that added them.

- `001` through `008` are the existing schema. Skip a file only when that migration is already present.
- `009_billing_authority.sql` revokes browser writes to `users.tier`, subscriptions, payments, admin stats, and direct calendar-share writes. It does not delete rows. Profile edits go through `PATCH /api/auth/me`, which uses the service role. Confirm with a disposable account that a logged-in user can still read their profile and cannot `update users set tier = 'admin'`.
- `010_pro_access_period.sql` adds `users.pro_expires_at` and `payments.access_until`. For each existing `tier = 'pro'` user who has a paid payment, it sets expiry to the later of (latest payment + 30 days) and (now + 7 days). Complimentary Pro with no payment is left with a null expiry and keeps access. Admin rows are not changed. Users whose paid period is already over get 7 days from the migration, then the API treats them as free on the next request. The unique index on the old `paystack_reference` column fails the migration if duplicate references already exist; inspect those rows before retrying.
- `011_master_settings_source.sql` copies an active cycle, active constraints, and leave blocks into Master Settings only when that section is empty. Apply it before launch so existing accounts already have one calendar document. The API also reads those tables when the document section is empty, so a short delay does not drop data. Running it twice does not replace a section that was already filled.
- `012_proposal_expiry.sql` adds `proposals.expires_at` and an index when the column is missing. Migration 002 only creates that column for a new table, so databases created before it was in the `CREATE TABLE` statement reject proposal inserts with PGRST204. The statement is idempotent. It does not rewrite rows.

Take a database backup before 009, 010, and 011.

## 3. Render

`render.yaml` describes one **free** Python web service. Health check path is `/health`. `autoDeploy` is false so the first boot is started from the dashboard.

1. In Render, create a Blueprint from this repo, or create a web service manually with:
   - Instance type: **Free**
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - Health check path: `/health`
   - Python 3.12 (`runtime.txt`)
2. Paste the secrets from the table above. Leave secret values as dashboard secrets, not in git.
3. Deploy the commit that contains this file and the merged security branches.
4. `GET https://<your-service>.onrender.com/health` returns `{"status":"healthy",...}` with no authentication.
5. In Stripe → Developers → Webhooks, add the endpoint `https://<your-service>.onrender.com/api/payments/webhook` with the events `checkout.session.completed`, `invoice.paid`, `invoice.payment_failed`, `customer.subscription.created`, `customer.subscription.updated` and `customer.subscription.deleted`. Copy its signing secret into `STRIPE_WEBHOOK_SECRET`. A bad signature returns 400.
   In Stripe → Settings → Billing → Customer portal, save the settings once (allow cancelling, switching between the monthly and yearly prices, and updating the payment method). Manage billing opens this portal.
6. Point the Vercel project's `NEXT_PUBLIC_API_URL` at the new origin and redeploy the client. The client still defaults to the deleted Render host when that variable is empty.

The free instance sleeps after a period without traffic. The first request after sleep can take up to a minute while Render starts the process. `GET /health` is the check that process uses. This service does not ping itself.

## 4. Docker

```bash
docker build -t watchman-api .
docker run --env-file .env -p 8000:8000 watchman-api
curl -f http://127.0.0.1:8000/health
```

`.env` is the filled copy of `.env.example`. The image does not contain it.

## 5. Cron

`POST /api/cron/weekly-summary` with header `X-Cron-Secret: <CRON_SECRET>`.

Without `CRON_SECRET` the route returns 503. A wrong secret returns 401. The old behavior of reusing the first 32 characters of the service role key, or the literal `dev-cron-secret`, is gone.

## 6. After the first boot

- In Stripe test mode, upgrade a disposable account with the card `4242 4242 4242 4242`. Confirm the account turns Pro with `pro_expires_at` at the end of the billing period, that Manage billing opens, and that cancelling keeps Pro until that date.
- Confirm a free user receives 403 from `GET /api/daily-logs/export` and `GET /api/incidents/export`.
- Read `docs/LAUNCH_DECISIONS.md` before merging the calendar tables.
