-- Add exercises JSONB column to planned_sessions for structured strength data
ALTER TABLE planned_sessions ADD COLUMN IF NOT EXISTS exercises JSONB;

-- Example exercises format:
-- [
--   {"name": "Knäböj", "sets": 3, "reps": 12, "weight_from": 40, "weight_to": 60},
--   {"name": "Utfall", "sets": 3, "reps": 10, "weight_from": 20, "weight_to": 20},
--   {"name": "Rygglyft", "sets": 3, "reps": 15},
--   {"name": "Plankan", "sets": 3, "reps": 45, "unit": "s"}
-- ]
