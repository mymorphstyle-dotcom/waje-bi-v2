CREATE SCHEMA IF NOT EXISTS waje_runtime;

CREATE TABLE IF NOT EXISTS waje_runtime.investigation_threads (
  thread_id text PRIMARY KEY,
  owner_id text NOT NULL,
  current_topic_id text,
  pending_clarification_topic_id text,
  pending_clarification_id text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.conversation_topics (
  topic_id text PRIMARY KEY,
  thread_id text NOT NULL REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  title text NOT NULL,
  summary text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'active',
  assumptions jsonb NOT NULL DEFAULT '[]'::jsonb,
  open_questions jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.conversation_turns (
  turn_id text PRIMARY KEY,
  thread_id text NOT NULL REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  topic_id text REFERENCES waje_runtime.conversation_topics(topic_id) ON DELETE SET NULL,
  intent text NOT NULL DEFAULT '',
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.conversation_messages (
  message_id text PRIMARY KEY,
  thread_id text NOT NULL REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  turn_id text REFERENCES waje_runtime.conversation_turns(turn_id) ON DELETE SET NULL,
  role text NOT NULL,
  text text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.analysis_runs (
  run_id text PRIMARY KEY,
  thread_id text NOT NULL REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  turn_id text REFERENCES waje_runtime.conversation_turns(turn_id) ON DELETE SET NULL,
  topic_id text REFERENCES waje_runtime.conversation_topics(topic_id) ON DELETE SET NULL,
  status text NOT NULL,
  request jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.run_nodes (
  node_id text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  node_name text NOT NULL,
  status text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz,
  finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS waje_runtime.context_manifests (
  manifest_id text PRIMARY KEY,
  thread_id text NOT NULL REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  turn_id text REFERENCES waje_runtime.conversation_turns(turn_id) ON DELETE SET NULL,
  can_support_claims boolean NOT NULL,
  items jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.result_refs (
  result_ref text PRIMARY KEY,
  topic_id text NOT NULL REFERENCES waje_runtime.conversation_topics(topic_id) ON DELETE CASCADE,
  snapshot_id text NOT NULL,
  contract_version text NOT NULL,
  permission_scope text NOT NULL,
  semantic_scope text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.evidence_refs (
  evidence_ref text PRIMARY KEY,
  run_id text REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  result_ref text REFERENCES waje_runtime.result_refs(result_ref) ON DELETE SET NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.answer_packages (
  package_id text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  artifact_id text,
  status text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.analysis_assets (
  asset_id text PRIMARY KEY,
  thread_id text NOT NULL REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  topic_id text NOT NULL REFERENCES waje_runtime.conversation_topics(topic_id) ON DELETE CASCADE,
  asset_type text NOT NULL,
  status text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.investigation_artifacts (
  artifact_id text PRIMARY KEY,
  thread_id text NOT NULL REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  topic_id text NOT NULL REFERENCES waje_runtime.conversation_topics(topic_id) ON DELETE CASCADE,
  run_id text REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE SET NULL,
  snapshot_id text NOT NULL,
  permission_scope text NOT NULL,
  follow_up_context text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.memory_items (
  memory_id text PRIMARY KEY,
  owner_scope text NOT NULL,
  text text NOT NULL,
  source_ref text NOT NULL,
  visibility text NOT NULL,
  status text NOT NULL,
  ttl text NOT NULL DEFAULT 'until_revoked',
  confidence text NOT NULL DEFAULT 'user_confirmed',
  refresh_rule text NOT NULL DEFAULT 'refresh_on_contract_or_scope_change',
  revocation_path text NOT NULL DEFAULT 'memory_proposal_revoke_or_admin_action',
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz
);

ALTER TABLE waje_runtime.memory_items
  ADD COLUMN IF NOT EXISTS refresh_rule text NOT NULL DEFAULT 'refresh_on_contract_or_scope_change';

ALTER TABLE waje_runtime.memory_items
  ADD COLUMN IF NOT EXISTS revocation_path text NOT NULL DEFAULT 'memory_proposal_revoke_or_admin_action';

CREATE TABLE IF NOT EXISTS waje_runtime.memory_proposals (
  proposal_id text PRIMARY KEY,
  thread_id text NOT NULL REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  text text NOT NULL,
  source_ref text NOT NULL,
  owner_scope text NOT NULL,
  visibility text NOT NULL,
  status text NOT NULL DEFAULT 'proposed',
  created_at timestamptz NOT NULL DEFAULT now(),
  decided_at timestamptz
);

CREATE TABLE IF NOT EXISTS waje_runtime.audit_events (
  audit_id bigserial PRIMARY KEY,
  event_type text NOT NULL,
  actor_id text NOT NULL DEFAULT '',
  thread_id text,
  topic_id text,
  run_id text,
  ref text,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.dataset_snapshots (
  snapshot_ref text PRIMARY KEY,
  dataset_id text NOT NULL,
  physical_table text NOT NULL,
  watermark date NOT NULL,
  schema_fingerprint text NOT NULL,
  schema_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
  contract_ref text NOT NULL,
  permission_scopes jsonb NOT NULL DEFAULT '[]'::jsonb,
  loaded_at timestamptz NOT NULL,
  status text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conversation_topics_thread ON waje_runtime.conversation_topics(thread_id);
CREATE INDEX IF NOT EXISTS idx_conversation_turns_thread ON waje_runtime.conversation_turns(thread_id);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_thread ON waje_runtime.analysis_runs(thread_id);
CREATE INDEX IF NOT EXISTS idx_result_refs_topic ON waje_runtime.result_refs(topic_id);
CREATE INDEX IF NOT EXISTS idx_analysis_assets_topic ON waje_runtime.analysis_assets(topic_id, created_at);
CREATE INDEX IF NOT EXISTS idx_investigation_artifacts_topic ON waje_runtime.investigation_artifacts(topic_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_events_thread ON waje_runtime.audit_events(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dataset_snapshots_lookup
  ON waje_runtime.dataset_snapshots(dataset_id, status, loaded_at DESC);
