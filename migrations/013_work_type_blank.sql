-- Calendar days can be marked untracked.
-- Already applied in production. Safe to run more than once.
-- ADD VALUE is not replayed inside the same transaction that uses the new label.

ALTER TYPE public.work_type ADD VALUE IF NOT EXISTS 'blank';
