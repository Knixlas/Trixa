-- Goal fields on profiles
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS vision TEXT;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS season_goal TEXT;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS short_term_goal TEXT;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS goal_updated_at TIMESTAMPTZ;
