-- Fresh databases created from migration 002 use the label 'rejected'.
-- Production uses 'declined' and does not have 'rejected'. The review route
-- stores 'declined'. This adds that label where it is missing.
--
-- Chat can also send commitments.type = 'other'. That label is not in the
-- original commitment_type enum. add_leave does not write this column; its
-- type stays in leave_blocks.effects.
--
-- Safe to run more than once. Production already has 'declined', so the
-- proposal_status statement is a no-op there.

ALTER TYPE public.proposal_status ADD VALUE IF NOT EXISTS 'declined';
ALTER TYPE public.proposal_status ADD VALUE IF NOT EXISTS 'expired';
ALTER TYPE public.commitment_type ADD VALUE IF NOT EXISTS 'other';
