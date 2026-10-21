-- Copy relational calendar rows into Master Settings only where that
-- section is still empty. Safe to run more than once. Does not delete rows
-- and does not replace a cycle, leave list, or constraint list already stored.
BEGIN;

INSERT INTO public.master_settings (user_id, settings, version)
SELECT
    cycles.user_id,
    jsonb_build_object(
        'cycle', jsonb_build_object(
            'id', cycles.id,
            'name', cycles.name,
            'pattern', cycles.pattern,
            'anchor_date', cycles.anchor_date,
            'anchor_cycle_day', cycles.anchor_cycle_day,
            'total_days', cycles.cycle_length
        ),
        'work', jsonb_build_object(
            'shift_hours', 12,
            'shift_start', '06:00',
            'shift_end', '18:00',
            'break_minutes', 60
        ),
        'constraints', '[]'::jsonb,
        'commitments', '[]'::jsonb,
        'leave_blocks', '[]'::jsonb,
        'preferences', jsonb_build_object(
            'timezone', 'UTC',
            'week_starts_on', 'monday',
            'theme', 'dark',
            'notifications', true
        )
    ),
    1
FROM (
    SELECT DISTINCT ON (user_id)
        id, user_id, name, pattern, anchor_date, anchor_cycle_day, cycle_length
    FROM public.cycles
    WHERE is_active IS TRUE
    ORDER BY user_id, updated_at DESC NULLS LAST
) AS cycles
WHERE NOT EXISTS (
    SELECT 1 FROM public.master_settings existing
    WHERE existing.user_id = cycles.user_id
);

UPDATE public.master_settings AS settings
SET settings = jsonb_set(
    settings.settings,
    '{cycle}',
    jsonb_build_object(
        'id', cycles.id,
        'name', cycles.name,
        'pattern', cycles.pattern,
        'anchor_date', cycles.anchor_date,
        'anchor_cycle_day', cycles.anchor_cycle_day,
        'total_days', cycles.cycle_length
    ),
    true
)
FROM (
    SELECT DISTINCT ON (user_id)
        id, user_id, name, pattern, anchor_date, anchor_cycle_day, cycle_length
    FROM public.cycles
    WHERE is_active IS TRUE
    ORDER BY user_id, updated_at DESC NULLS LAST
) AS cycles
WHERE cycles.user_id = settings.user_id
  AND (
      settings.settings->'cycle' IS NULL
      OR settings.settings->'cycle' = 'null'::jsonb
  );

UPDATE public.master_settings AS settings
SET settings = jsonb_set(
    settings.settings,
    '{constraints}',
    rules.constraints,
    true
)
FROM (
    SELECT
        user_id,
        jsonb_agg(jsonb_build_object(
            'id', id,
            'name', name,
            'description', description,
            'rule', rule,
            'active', is_active,
            'is_system', is_system
        ) ORDER BY created_at) AS constraints
    FROM public.constraints
    WHERE is_active IS TRUE
    GROUP BY user_id
) AS rules
WHERE rules.user_id = settings.user_id
  AND (
      settings.settings->'constraints' IS NULL
      OR settings.settings->'constraints' = 'null'::jsonb
      OR settings.settings->'constraints' = '[]'::jsonb
  );

UPDATE public.master_settings AS settings
SET settings = jsonb_set(
    settings.settings,
    '{leave_blocks}',
    leaves.leave_blocks,
    true
)
FROM (
    SELECT
        user_id,
        jsonb_agg(jsonb_build_object(
            'id', id,
            'name', name,
            'start_date', start_date,
            'end_date', end_date,
            'notes', notes
        ) ORDER BY start_date) AS leave_blocks
    FROM public.leave_blocks
    GROUP BY user_id
) AS leaves
WHERE leaves.user_id = settings.user_id
  AND (
      settings.settings->'leave_blocks' IS NULL
      OR settings.settings->'leave_blocks' = 'null'::jsonb
      OR settings.settings->'leave_blocks' = '[]'::jsonb
  );

COMMIT;
