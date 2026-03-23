-- Flexible training log - Trixa fills in what she can from any source
CREATE TABLE IF NOT EXISTS training_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    date            DATE NOT NULL,
    sport           TEXT NOT NULL,           -- run, bike, swim, strength, other
    title           TEXT,                    -- "Tröskelintervaller 5x1km"
    duration_min    REAL,
    distance_km     REAL,
    avg_hr          INTEGER,
    max_hr          INTEGER,
    avg_power       INTEGER,
    normalized_power INTEGER,
    pace            TEXT,                    -- "5:45/km"
    tss             REAL,
    rpe             INTEGER,                -- 1-10 perceived exertion
    feeling         TEXT,                    -- "bra", "tungt", "fantastiskt"
    notes           TEXT,                    -- free text from athlete
    coach_notes     TEXT,                    -- Trixa's analysis of the session
    source          TEXT DEFAULT 'chat',     -- chat, garmin_screenshot, strava, manual
    strava_id       BIGINT,                 -- link to strava_activities if synced
    planned_session_id UUID,                -- link to planned_sessions if matched
    rating          INTEGER,                -- 1-5 stars
    extra_data      JSONB DEFAULT '{}',     -- dynamic fields Trixa deems relevant
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- Index for queries
CREATE INDEX IF NOT EXISTS idx_training_log_user_date
    ON training_log (user_id, date DESC);

-- RLS
ALTER TABLE training_log ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users read own training log" ON training_log;
CREATE POLICY "Users read own training log" ON training_log
    FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Service manages training log" ON training_log;
CREATE POLICY "Service manages training log" ON training_log
    FOR ALL USING (true) WITH CHECK (true);

-- Sync existing Strava activities into training_log
-- (Run once to backfill, then Strava sync also writes here)
INSERT INTO training_log (user_id, date, sport, title, duration_min, distance_km, avg_hr, avg_power, pace, source, strava_id, rating)
SELECT
    user_id, date, type, name, duration_min, distance_km, avg_hr, avg_power, pace, 'strava', strava_id, rating
FROM strava_activities
ON CONFLICT DO NOTHING;
