# Deploy Watchman API

The previous Render service (`watchman-api-dnm0.onrender.com`) is gone. This is a new host. Do not point a deploy at the production Supabase database until migrations 009 and 010 have been reviewed and applied by someone with database-owner access.

Merge order before the first production deploy:

1. `fix/security-user-isolation`
2. `fix/chat-history-and-approval`
3. `fix/database-billing-authority`
4. `cursor/entitlements-expiry-ea99`
5. `cursor/test-suite-triage-ea99`
6. `cursor/deploy-readiness-ea99` (this file and `docs/LAUNCH_DECISIONS.md`)

## 1. Secrets

Set these on the host. Names match `.env.example`.

| Variable | Where to get it |
| --- | --- |
| `SUPABASE_URL` | Supabase → Project Settings → API → Project URL |
| `SUPABASE_ANON_KEY` | Same page → anon public key |
| `SUPABASE_SERVICE_KEY` | Same page → service_role key. Server only. |
| `SUPABASE_JWT_SECRET` | Project Settings → API → JWT Secret |
| `GEMINI_API_KEY` | Google AI Studio / Gemini API key |
| `PAYSTACK_SECRET_KEY` | Paystack → Settings → API Keys & Webhooks. This key also verifies webhooks. |
| `PAYSTACK_PUBLIC_KEY` | Same page. Not used to verify webhooks. |
| `EXCHANGE_RATE_API_KEY` | exchangerate-api.com. Checkout returns 503 without a live USD→GHS rate. |
| `RESEND_API_KEY` | Resend dashboard |
| `EMAIL_FROM` | `Watchman <notifications@trywatchman.app>` once that domain is verified in Resend |
| `CRON_SECRET` | A new long random string. Send it as the `X-Cron-Secret` header. |
| `CORS_ORIGINS` | The frontend origins, comma-separated, no spaces required but spaces are trimmed |
| `APP_ENV` | `production` |
| `DEBUG` | `false` |

There is no Stripe key. Do not set `STRIPE_*`.

`PAYSTACK_WEBHOOK_SECRET` is unused. Paystack signs the raw body with `PAYSTACK_SECRET_KEY` (HMAC SHA-512). The API rejects webhooks when that key or the signature is missing.

The frontend on Vercel needs `NEXT_PUBLIC_API_URL` set to this service's public URL. That value lives in `watchman-client`, not here.

## 2. Database

Apply the SQL files in `migrations/` in filename order, in the Supabase SQL editor, as the database owner. Merging this repository does not run SQL.

Stop after reviewing each statement. None of these statements were run against production from the change that added them.

- `001` through `008` are the existing schema. Skip a file only when that migration is already present.
- `009_billing_authority.sql` revokes browser writes to `users.tier`, subscriptions, payments, admin stats, and direct calendar-share writes. It does not delete rows. Profile edits go through `PATCH /api/auth/me`, which uses the service role. Confirm with a disposable account that a logged-in user can still read their profile and cannot `update users set tier = 'admin'`.
- `010_pro_access_period.sql` adds `users.pro_expires_at` and `payments.access_until`. For each existing `tier = 'pro'` user who has a paid payment, it sets expiry to the later of (latest payment + 30 days) and (now + 7 days). Complimentary Pro with no payment is left with a null expiry and keeps access. Admin rows are not changed. Users whose paid period is already over get 7 days from the migration, then the API treats them as free on the next request. The unique index on `paystack_reference` fails the migration if duplicate references already exist; inspect those rows before retrying.

Take a database backup before 009 and 010.

## 3. Render

`render.yaml` describes one Python web service, plan starter, health check `GET /health`. `autoDeploy` is false so the first boot does not race an unmerged branch.

1. In Render, create a Blueprint from this repo, or create a web service manually with:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - Health check path: `/health`
   - Python 3.12 (`runtime.txt`)
2. Paste the secrets from the table above. Leave secret values as dashboard secrets, not in git.
3. Deploy the commit that contains this file and the merged security branches.
4. `GET https://<your-service>.onrender.com/health` returns `{"status":"healthy",...}` with no authentication.
5. In Paystack, set the webhook URL to `https://<your-service>.onrender.com/api/payments/webhook` and send a test event. A bad signature returns 401. Do not grant Pro from the Paystack dashboard by hand.
6. Point the Vercel project's `NEXT_PUBLIC_API_URL` at the new origin and redeploy the client. The client still defaults to the deleted Render host when that variable is empty.

The starter plan does not sleep. This service does not ping the deleted host.

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

- Create a Paystack webhook and a disposable checkout. Confirm the user receives `pro_expires_at` about 30 days out and that repeating the same webhook does not add another 30 days.
- Confirm a free user receives 403 from `GET /api/daily-logs/export` and `GET /api/incidents/export`.
- Read `docs/LAUNCH_DECISIONS.md` before building Paystack subscription plans or merging the calendar tables.
