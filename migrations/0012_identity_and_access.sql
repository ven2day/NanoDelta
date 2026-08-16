CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE IF NOT EXISTS auth.users (
    user_id uuid PRIMARY KEY,
    username text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role text NOT NULL CHECK (role IN ('viewer', 'operator', 'admin')),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    password_changed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS auth.login_throttle (
    username text NOT NULL,
    source_hash text NOT NULL,
    failed_attempts integer NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    window_started_at timestamptz NOT NULL DEFAULT now(),
    locked_until timestamptz,
    PRIMARY KEY (username, source_hash)
);

CREATE TABLE IF NOT EXISTS auth.sessions (
    session_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth.users(user_id),
    token_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    source_hash text NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_sessions_user_active_idx
    ON auth.sessions(user_id, expires_at DESC) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS auth.api_keys (
    key_id uuid PRIMARY KEY,
    name text NOT NULL UNIQUE,
    key_hash text NOT NULL UNIQUE,
    actor_id text NOT NULL,
    role text NOT NULL CHECK (role IN ('viewer', 'operator', 'admin')),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    revoked_at timestamptz,
    last_used_at timestamptz
);

CREATE TABLE IF NOT EXISTS auth.security_audit (
    event_id uuid PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    event_type text NOT NULL,
    actor_id text,
    subject_id text,
    source_hash text,
    success boolean NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS security_audit_time_idx
    ON auth.security_audit(occurred_at DESC);
