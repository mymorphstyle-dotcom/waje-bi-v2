CREATE SCHEMA IF NOT EXISTS waje_runtime;

CREATE TABLE IF NOT EXISTS waje_runtime.contract_artifacts (
  path text PRIMARY KEY,
  sha256 text NOT NULL,
  yaml_text text NOT NULL,
  mirrored_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.active_contracts (
  path text PRIMARY KEY REFERENCES waje_runtime.contract_artifacts(path) ON DELETE CASCADE,
  activated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.mirror_loads (
  load_id bigserial PRIMARY KEY,
  loaded_at timestamptz NOT NULL DEFAULT now(),
  artifact_count integer NOT NULL,
  note text
);
