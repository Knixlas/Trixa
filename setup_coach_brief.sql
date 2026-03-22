-- Coach brief cache (one per user per day)
CREATE TABLE IF NOT EXISTS coach_briefs (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    brief_date DATE NOT NULL DEFAULT CURRENT_DATE,
    brief TEXT NOT NULL,
    follow_up TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, brief_date)
);

-- RLS
ALTER TABLE coach_briefs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users read own briefs" ON coach_briefs;
CREATE POLICY "Users read own briefs" ON coach_briefs
    FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Service role manages briefs" ON coach_briefs;
CREATE POLICY "Service role manages briefs" ON coach_briefs
    FOR ALL USING (auth.role() = 'service_role');

-- Coach memory table (if not exists)
CREATE TABLE IF NOT EXISTS coach_memory (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    observation TEXT NOT NULL,
    confidence REAL DEFAULT 0.7,
    times_seen INTEGER DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT now(),
    last_seen TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE coach_memory ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users read own memory" ON coach_memory;
CREATE POLICY "Users read own memory" ON coach_memory
    FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Service role manages memory" ON coach_memory;
CREATE POLICY "Service role manages memory" ON coach_memory
    FOR ALL USING (auth.role() = 'service_role');
