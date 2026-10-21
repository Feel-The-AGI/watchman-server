-- Production review failed with:
--   invalid input value for enum proposal_status: "rejected"
-- The proposals table was created before that label existed, and CREATE TYPE
-- does not add it later. Decline sets status to rejected, so the update
-- errors and the row stays pending.
--
-- Chat can also send commitments.type = 'other'. That label is not in the
-- original commitment_type enum. add_leave does not write this column; its
-- type stays in leave_blocks.effects.
--
-- Safe to run more than once. Do not apply this from CI or a deploy.
-- Run it in the Supabase SQL editor:
--
--   ALTER TYPE public.proposal_status ADD VALUE IF NOT EXISTS 'rejected';
--   ALTER TYPE public.proposal_status ADD VALUE IF NOT EXISTS 'expired';
--   ALTER TYPE public.commitment_type ADD VALUE IF NOT EXISTS 'other';

ALTER TYPE public.proposal_status ADD VALUE IF NOT EXISTS 'rejected';
ALTER TYPE public.proposal_status ADD VALUE IF NOT EXISTS 'expired';
ALTER TYPE public.commitment_type ADD VALUE IF NOT EXISTS 'other';
