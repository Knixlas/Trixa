-- Persistent training plan
CREATE TABLE IF NOT EXISTS planned_sessions (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    sport TEXT,           -- Lopning, Cykel, Sim, Styrka, Vila, Brick
    title TEXT NOT NULL,  -- "Lop 50min Z2" or "Vila"
    details TEXT,         -- Zone info, intervals, purpose
    purpose TEXT,         -- Why this session
    status TEXT DEFAULT 'planned',  -- planned / completed / skipped / adjusted
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, date, sport)
);

ALTER TABLE planned_sessions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users read own plan" ON planned_sessions;
CREATE POLICY "Users read own plan" ON planned_sessions
    FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Service role manages plan" ON planned_sessions;
CREATE POLICY "Service role manages plan" ON planned_sessions
    FOR ALL USING (auth.role() = 'service_role');
