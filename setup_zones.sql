-- Add training zone fields to profiles
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS ftp INTEGER;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS css_per_100m TEXT;  -- e.g. "1:45"
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS threshold_pace TEXT;  -- e.g. "4:30/km"
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS threshold_hr INTEGER;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS max_hr INTEGER;
