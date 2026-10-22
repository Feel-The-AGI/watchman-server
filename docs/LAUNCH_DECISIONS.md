# Launch decisions left open

Two designs were not rewritten. Each one is verified against the current code, with the options and a recommendation.

## Recurring billing

### What is true today

Checkout calls Paystack `POST /transaction/initialize`. That is a one-off charge in GHS, not a Paystack plan or subscription. The amount is a fixed plan price: monthly GHS 70 for 30 days, yearly GHS 560 for 365 days. USD figures are display only and are never charged. Checkout does not use a live exchange rate.

A `charge.success` webhook grants Pro only when all of these hold:

- the raw body has a valid HMAC-SHA512 signature from `PAYSTACK_SECRET_KEY`;
- Paystack's verify API reports `success` for that reference;
- the reference was stored by our checkout as `pending`;
- the charged currency is GHS and the pesewa amount matches the stored price.

The grant is that plan's days, or 30 days when a legacy checkout has no plan, from the later of now and the current `users.pro_expires_at`. The same reference does not extend the period again. Daily-log export, incident export, and the existing stats export require current Pro. Admin accounts are not counted in MRR.

Cancellation of a paid period sets `cancel_at_period_end` and leaves access until `pro_expires_at`. A Paystack `subscription.disable` event does the same. Failed renewal marks the subscription `past_due` and does not cut off a period that was already paid.

This is prepaid access, not a subscription that renews itself. The client copy still says the plan is monthly and the legal pages in `watchman-client` still name Stripe. Those client changes are outside this repository.

`subscriptions` rows are updated so the settings screen, which already reads `current_period_end`, can show the period. They are not Paystack plan subscriptions.

### Options

1. **Keep prepaid 30-day charges.** Checkout stays as it is. The user pays again when the period ends. No card-on-file renewal, no dunning. The in-app "cancel" only means "do not expect another charge" unless a Paystack subscription code already exists.
2. **Paystack Plans.** Create a GHS plan in the Paystack dashboard and initialize a subscription instead of a one-off transaction. Paystack then sends `invoice.update`, `invoice.payment_failed`, and `subscription.disable`, which this API already records without granting access from an invoice that we did not create. Renewal can extend `pro_expires_at` by the plan interval after the same amount check. Price the plan in GHS directly so a failed FX call cannot block renewal. Review that GHS price when the dollar price needs to move.
3. **A merchant of record (Lemon Squeezy or Paddle).** They collect tax and handle cards outside Ghana. This replaces Paystack, the webhook, the GHS conversion, and the client checkout. It is a new vendor and a new review of who the mining/FIFO customers can actually pay.

### Recommendation

Stay on Paystack. The product's first customers pay in Ghana, and the server already speaks Paystack. Move from one-off charges to a **Paystack plan priced in GHS** when renewal should be automatic. Do not adopt a merchant of record unless the audience moves out of Ghana or tax handling becomes the larger problem. Until that plan exists, the 30-day prepaid period is the behavior to ship: one payment is not indefinite Pro, and a missing exchange rate does not invent a price.

## Two calendar sources

### What is true today

The calendar the app shows is `calendar_days`. Two different inputs regenerate it.

- **Relational tables.** `POST /api/cycles` and the calendar generate route read `cycles` and `leave_blocks`. Updating a cycle copies that cycle into `master_settings` and regenerates `calendar_days` from the cycle row.
- **Master Settings JSON.** Chat and `/api/commands` read and write one JSON document: cycle, commitments, leave, and constraints. After an approved command, the executor regenerates `calendar_days` from that JSON. It does not write the `cycles` table.

A chat change and a later "regenerate from the cycles table" can overwrite each other. Undo and redo restore the JSON snapshot, not the relational rows.

Constraints are split the same way. Onboarding inserts relational rules such as `{"type": "no_activity_on", "activity": "study", "work_types": ["work_night"]}`. The command validator does not read that table. It only inspects JSON constraints whose `rule` is the string `"no_study_on_night_shift"`. That branch does not look at calendar days. The comment in `command_executor._check_constraints` says the real day check is still to be done. A hard constraint of that exact string blocks every education commitment. Any other rule, including the system rule created at onboarding, is ignored by the agent path. Overlapping leave is a warning.

Free-tier numbers that are enforced on the server match the client settings table: 100 chat messages per month, 50 history rows, one cycle on the cycles API, and a 6-month generate window. The chat panel's `messageLimit` of 50 is the history window, not a second monthly cap.

### Options

1. **Make Master Settings the only editable source.** REST cycle, commitment, leave, and constraint routes read and write the JSON document, and `calendar_days` stays the generated projection. Relational tables become a one-time import. Undo, approval, and the agent already use this document.
2. **Make the relational tables the only editable source.** The command executor writes `cycles`, `commitments`, `leave_blocks`, and `constraints`, then regenerates from those rows. Master Settings becomes a cached view. This keeps SQL constraints and the existing REST routes, and it throws away the JSON snapshot model the agent uses for undo.
3. **Sync both ways.** Every writer updates both stores. This is the smallest patch and the easiest to get wrong: two shapes (nested anchor vs flat anchor, JSON rule vs string rule) already exist, and a missed writer recreates the overwrite.

### Recommendation

Choose **Master Settings as the editable source** and keep `calendar_days` as the projection. The conversational product, proposals, and undo already live there.

That read-through is now in place. `POST /api/cycles` and `PATCH /api/cycles/{id}` copy the saved rotation into Master Settings and write `calendar_days` once, which is the only write the client performs. `POST /api/calendar/generate` reads Master Settings and does not replace that cycle with a different row from the cycles table. The night-shift rule saved as `{type: "no_study_on", value: "work_night"}` is read by the agent and by day generation, as are the older string and `no_activity_on` shapes. It blocks study only on dates that are actually night shifts. Migration 011 copies empty document sections once and does not delete rows. The relational tables are still present.

Neither store is deleted. A later cleanup can drop the relational copies only after comparing them with the document.
