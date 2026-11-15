# Launch decisions left open

Two designs were open. Billing is now decided. Each one is verified against the current code, with the options and a recommendation.

## Recurring billing (decided: Stripe subscriptions)

Pro is a Stripe subscription: $12 a month or $96 a year in USD, set by `PRO_PRICE_USD` and `PRO_ANNUAL_PRICE_USD`. The server finds or creates the product and the two prices by lookup key (`watchman_pro_monthly`, `watchman_pro_annual`) on the first checkout. Changing an amount creates a new price for new subscribers; existing subscribers keep theirs.

Access only comes from Stripe:

- `invoice.paid`, `checkout.session.completed` and `customer.subscription.*` webhooks, with a valid `Stripe-Signature` checked against `STRIPE_WEBHOOK_SECRET`;
- or the return from checkout, where the server fetches the session itself and checks it belongs to the signed-in user.

Each case reads the subscription from Stripe and sets `users.pro_expires_at` to the end of the paid period plus two days of grace. Access only moves forward: a late or repeated event never shortens it. Cancelling sets `cancel_at_period_end`; access lasts until the paid period ends, then the account is Free. A failed renewal marks the subscription `past_due` while Stripe retries.

Customers change card, switch plan, download invoices or cancel in the Stripe customer portal (Manage billing).

The old Paystack columns (`paystack_customer_code`, `paystack_subscription_code`, `payments.paystack_reference` and the GHS columns) are no longer read. They are left in place so no migration is needed; they can be dropped later.

## Two calendar sources

### What is true today

The calendar the app shows is `calendar_days`. Two different inputs regenerate it.

- **Relational tables.** `POST /api/cycles` and the calendar generate route read `cycles` and `leave_blocks`. Updating a cycle copies that cycle into `master_settings` and regenerates `calendar_days` from the cycle row.
- **Master Settings JSON.** Chat and `/api/commands` read and write one JSON document: cycle, commitments, leave, and constraints. After an approved command, the executor regenerates `calendar_days` from that JSON. It does not write the `cycles` table.

A chat change and a later "regenerate from the cycles table" can overwrite each other. Undo and redo restore the JSON snapshot, not the relational rows.

Constraints are split the same way. Onboarding inserts relational rules such as `{"type": "no_activity_on", "activity": "study", "work_types": ["work_night"]}`. The command validator does not read that table. It only inspects JSON constraints whose `rule` is the string `"no_study_on_night_shift"`. That branch does not look at calendar days. The comment in `command_executor._check_constraints` says the real day check is still to be done. A hard constraint of that exact string blocks every education commitment. Any other rule, including the system rule created at onboarding, is ignored by the agent path. Overlapping leave is a warning.

Free-tier numbers that are enforced on the server match the client settings table: 100 chat messages per month, 50 history rows, one cycle on the cycles API, and a 3-month generate window. The chat panel's `messageLimit` of 50 is the history window, not a second monthly cap.

### Options

1. **Make Master Settings the only editable source.** REST cycle, commitment, leave, and constraint routes read and write the JSON document, and `calendar_days` stays the generated projection. Relational tables become a one-time import. Undo, approval, and the agent already use this document.
2. **Make the relational tables the only editable source.** The command executor writes `cycles`, `commitments`, `leave_blocks`, and `constraints`, then regenerates from those rows. Master Settings becomes a cached view. This keeps SQL constraints and the existing REST routes, and it throws away the JSON snapshot model the agent uses for undo.
3. **Sync both ways.** Every writer updates both stores. This is the smallest patch and the easiest to get wrong: two shapes (nested anchor vs flat anchor, JSON rule vs string rule) already exist, and a missed writer recreates the overwrite.

### Recommendation

Choose **Master Settings as the editable source** and keep `calendar_days` as the projection. The conversational product, proposals, and undo already live there.

That read-through is now in place. `POST /api/cycles` and `PATCH /api/cycles/{id}` copy the saved rotation into Master Settings and write `calendar_days` once, which is the only write the client performs. `POST /api/calendar/generate` reads Master Settings and does not replace that cycle with a different row from the cycles table. The night-shift rule saved as `{type: "no_study_on", value: "work_night"}` is read by the agent and by day generation, as are the older string and `no_activity_on` shapes. It blocks study only on dates that are actually night shifts. Migration 011 copies empty document sections once and does not delete rows. The relational tables are still present.

Neither store is deleted. A later cleanup can drop the relational copies only after comparing them with the document.
