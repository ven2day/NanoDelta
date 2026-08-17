-- Shared cache for PIN+TOTP-generated Dhan access tokens. Every process that talks
-- to Dhan (api, runtime, history) resolves its own token independently; without a
-- shared cache, two processes starting within the same 30-second TOTP window submit
-- the same one-time code, and Dhan's replay protection accepts only the first.
CREATE TABLE IF NOT EXISTS control.dhan_access_tokens (
    client_id text PRIMARY KEY,
    access_token text NOT NULL,
    expires_at timestamptz NOT NULL,
    fetched_at timestamptz NOT NULL
);
