-- Security hardening (applied 2026-10-31 as migration security_hardening_rls_and_functions)

-- 1. "Service" policies were granted to role public with USING (true),
--    letting anyone with the anon key read/write every user's rows.
--    service_role bypasses RLS anyway, so restrict them to it.
alter policy "Service manages training log" on public.training_log to service_role;
alter policy "Service manages activities" on public.strava_activities to service_role;

-- 2. Views should respect the caller's RLS
alter view public.user_workout_preferences set (security_invoker = true);
alter view public.workout_type_ratings set (security_invoker = true);

-- 3. Trigger / event-trigger functions must not be callable via /rest/v1/rpc
revoke execute on function public.handle_new_user() from public, anon, authenticated;
revoke execute on function public.copy_email_to_profile() from public, anon, authenticated;
revoke execute on function public.training_log_dedup_strava() from public, anon, authenticated;
revoke execute on function public.rls_auto_enable() from public, anon, authenticated;

-- 4. Pin search_path on functions flagged as mutable
alter function public.is_coach_of(uuid) set search_path = public;
alter function public.update_updated_at() set search_path = public;
alter function public.update_subscription_updated_at() set search_path = public;
alter function public.override_run_tss_from_hr() set search_path = public;
alter function garmin_coach.set_updated_at() set search_path = garmin_coach, public;
