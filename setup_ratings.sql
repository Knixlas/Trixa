-- Add rating fields to strava_activities
ALTER TABLE strava_activities ADD COLUMN IF NOT EXISTS rating INTEGER;          -- 1-5 stars
ALTER TABLE strava_activities ADD COLUMN IF NOT EXISTS rating_comment TEXT;     -- optional short note
ALTER TABLE strava_activities ADD COLUMN IF NOT EXISTS rated_at TIMESTAMPTZ;

-- Aggregate stats view: which workout types are most liked
CREATE OR REPLACE VIEW workout_type_ratings AS
SELECT
    type,
    COUNT(*) FILTER (WHERE rating IS NOT NULL) AS total_rated,
    ROUND(AVG(rating) FILTER (WHERE rating IS NOT NULL), 1) AS avg_rating,
    COUNT(*) FILTER (WHERE rating >= 4) AS liked_count,
    COUNT(*) FILTER (WHERE rating <= 2) AS disliked_count
FROM strava_activities
GROUP BY type;

-- Per-user rating summary (for coach memory)
CREATE OR REPLACE VIEW user_workout_preferences AS
SELECT
    user_id,
    type,
    COUNT(*) FILTER (WHERE rating IS NOT NULL) AS times_rated,
    ROUND(AVG(rating) FILTER (WHERE rating IS NOT NULL), 1) AS avg_rating,
    COUNT(*) FILTER (WHERE rating >= 4) AS liked,
    COUNT(*) FILTER (WHERE rating <= 2) AS disliked
FROM strava_activities
GROUP BY user_id, type;
