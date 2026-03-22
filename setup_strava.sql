-- Strava integration tables for Trixa
-- Run in Supabase SQL Editor

-- Strava OAuth tokens (per user)
CREATE TABLE IF NOT EXISTS strava_tokens (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid UNIQUE NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    athlete_id  bigint NOT NULL,
    access_token text NOT NULL,
    refresh_token text NOT NULL,
    expires_at  bigint NOT NULL,
    scope       text,
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
);

-- RLS for strava_tokens
ALTER TABLE strava_tokens ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users read own strava tokens" ON strava_tokens;
CREATE POLICY "Users read own strava tokens" ON strava_tokens
    FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users update own strava tokens" ON strava_tokens;
CREATE POLICY "Users update own strava tokens" ON strava_tokens
    FOR UPDATE USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Service inserts strava tokens" ON strava_tokens;
CREATE POLICY "Service inserts strava tokens" ON strava_tokens
    FOR INSERT WITH CHECK (true);

-- Strava activities (synced from API)
CREATE TABLE IF NOT EXISTS strava_activities (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    strava_id   bigint NOT NULL UNIQUE,
    date        date NOT NULL,
    type        text NOT NULL,
    name        text,
    duration_min real,
    distance_km  real,
    avg_hr      integer,
    avg_power   integer,
    elevation_m real,
    pace        text,
    created_at  timestamptz DEFAULT now()
);

-- Index for dashboard queries
CREATE INDEX IF NOT EXISTS idx_strava_activities_user_date
    ON strava_activities (user_id, date DESC);

-- RLS for strava_activities
ALTER TABLE strava_activities ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users read own activities" ON strava_activities;
CREATE POLICY "Users read own activities" ON strava_activities
    FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Service manages activities" ON strava_activities;
CREATE POLICY "Service manages activities" ON strava_activities
    FOR ALL USING (true) WITH CHECK (true);
