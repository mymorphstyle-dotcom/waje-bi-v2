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

CREATE TABLE IF NOT EXISTS waje_runtime.run_dispatches (
  dispatch_id text PRIMARY KEY,
  producer_kind text NOT NULL
    CONSTRAINT run_dispatch_producer_kind_check
    CHECK (producer_kind IN (
      'thread_message', 'artifact_continue', 'clarification_resume'
    )),
  scope_ref text NOT NULL,
  request_identity text NOT NULL,
  request_digest text NOT NULL,
  request_payload jsonb NOT NULL,
  thread_id text NOT NULL REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  run_id text NOT NULL UNIQUE REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  message_id text NOT NULL UNIQUE REFERENCES waje_runtime.conversation_messages(message_id) ON DELETE CASCADE,
  dispatch_state text NOT NULL DEFAULT 'pending'
    CONSTRAINT run_dispatch_state_check
    CHECK (dispatch_state IN ('pending', 'leased', 'running', 'terminal')),
  owner_id text,
  lease_epoch bigint NOT NULL DEFAULT 0,
  lease_expires_at timestamptz,
  heartbeat_at timestamptz,
  terminal_status text,
  failure_reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(producer_kind, scope_ref, request_identity),
  CONSTRAINT run_dispatch_owner_shape_check CHECK (
    dispatch_state NOT IN ('leased', 'running')
    OR (owner_id IS NOT NULL AND lease_expires_at IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_run_dispatch_recovery
  ON waje_runtime.run_dispatches(dispatch_state, lease_expires_at)
  WHERE dispatch_state IN ('pending', 'leased', 'running');

CREATE TABLE IF NOT EXISTS waje_runtime.clarification_resume_claims (
  source_run_id text PRIMARY KEY REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  resumed_run_id text NOT NULL UNIQUE REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  thread_id text NOT NULL REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  request_identity text NOT NULL,
  submission jsonb NOT NULL,
  message_id text NOT NULL UNIQUE REFERENCES waje_runtime.conversation_messages(message_id) ON DELETE CASCADE,
  dispatch_state text NOT NULL DEFAULT 'pending'
    CONSTRAINT clarification_resume_dispatch_state_check
    CHECK (dispatch_state IN ('pending', 'leased', 'dispatched')),
  dispatch_owner_id text,
  dispatch_lease_expires_at timestamptz,
  dispatched_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE waje_runtime.clarification_resume_claims
  ADD COLUMN IF NOT EXISTS dispatch_state text NOT NULL DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS dispatch_owner_id text,
  ADD COLUMN IF NOT EXISTS dispatch_lease_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS dispatched_at timestamptz;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'clarification_resume_dispatch_state_check'
      AND conrelid = 'waje_runtime.clarification_resume_claims'::regclass
  ) THEN
    ALTER TABLE waje_runtime.clarification_resume_claims
      ADD CONSTRAINT clarification_resume_dispatch_state_check
      CHECK (dispatch_state IN ('pending', 'leased', 'dispatched'));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'clarification_resume_dispatch_lease_shape_check'
      AND conrelid = 'waje_runtime.clarification_resume_claims'::regclass
  ) THEN
    ALTER TABLE waje_runtime.clarification_resume_claims
      ADD CONSTRAINT clarification_resume_dispatch_lease_shape_check
      CHECK (
        dispatch_state <> 'leased'
        OR (
          dispatch_owner_id IS NOT NULL
          AND dispatch_lease_expires_at IS NOT NULL
        )
      );
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_clarification_resume_dispatch_recovery
  ON waje_runtime.clarification_resume_claims(dispatch_state, dispatch_lease_expires_at);

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
  logical_snapshot_id text NOT NULL DEFAULT '',
  load_revision text NOT NULL DEFAULT '',
  evidence_state text NOT NULL DEFAULT 'claim_ready',
  reconciliation_status text NOT NULL DEFAULT 'not_applicable',
  reconciliation_ref text NOT NULL DEFAULT '',
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE waje_runtime.dataset_snapshots
  ADD COLUMN IF NOT EXISTS logical_snapshot_id text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS load_revision text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS evidence_state text NOT NULL DEFAULT 'claim_ready',
  ADD COLUMN IF NOT EXISTS reconciliation_status text NOT NULL DEFAULT 'not_applicable',
  ADD COLUMN IF NOT EXISTS reconciliation_ref text NOT NULL DEFAULT '';

UPDATE waje_runtime.dataset_snapshots
SET logical_snapshot_id = COALESCE(
      NULLIF(payload->>'logical_snapshot_id', ''),
      NULLIF(payload->>'snapshot_id', ''),
      logical_snapshot_id
    ),
    load_revision = COALESCE(NULLIF(payload->>'load_revision', ''), load_revision),
    evidence_state = COALESCE(NULLIF(payload->>'evidence_state', ''), evidence_state),
    reconciliation_status = COALESCE(
      NULLIF(payload->>'reconciliation_status', ''),
      NULLIF(payload#>>'{reconciliation,status}', ''),
      reconciliation_status
    ),
    reconciliation_ref = COALESCE(
      NULLIF(payload->>'reconciliation_ref', ''),
      reconciliation_ref
    )
WHERE logical_snapshot_id = '' OR load_revision = '';

CREATE TABLE IF NOT EXISTS waje_runtime.dataset_snapshot_releases (
  release_ref text PRIMARY KEY,
  logical_snapshot_id text NOT NULL,
  load_revision text NOT NULL,
  snapshot_refs jsonb NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.analysis_contracts (
  analysis_contract_id text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  contract_signature text NOT NULL CHECK (length(contract_signature) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.query_contracts (
  query_contract_id text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  analysis_contract_id text NOT NULL REFERENCES waje_runtime.analysis_contracts(analysis_contract_id) ON DELETE CASCADE,
  contract_signature text NOT NULL CHECK (length(contract_signature) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.query_runs (
  result_ref text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  query_contract_id text NOT NULL REFERENCES waje_runtime.query_contracts(query_contract_id) ON DELETE CASCADE,
  execution_status text NOT NULL,
  query_hash text NOT NULL,
  rows_ref text NOT NULL,
  completeness_report_ref text NOT NULL,
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object' AND NOT (payload ? 'rows')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.snapshot_authority (
  record_ref text PRIMARY KEY,
  record_digest text NOT NULL,
  snapshot_ref text NOT NULL UNIQUE,
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.rows_metadata_authority (
  record_ref text PRIMARY KEY,
  record_digest text NOT NULL,
  rows_ref text NOT NULL UNIQUE,
  rows_content_hash text NOT NULL CHECK (length(rows_content_hash) = 64),
  row_count bigint NOT NULL CHECK (row_count >= 0),
  unique_key_fields jsonb NOT NULL CHECK (jsonb_typeof(unique_key_fields) = 'array'),
  storage_ref text NOT NULL,
  payload jsonb NOT NULL CHECK (
    jsonb_typeof(payload) = 'object'
    AND NOT (payload #> '{record}' ? 'rows')
  ),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.query_execution_authority (
  record_ref text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  record_digest text NOT NULL,
  result_ref text NOT NULL UNIQUE REFERENCES waje_runtime.query_runs(result_ref) ON DELETE CASCADE,
  query_contract_ref text NOT NULL REFERENCES waje_runtime.query_contracts(query_contract_id) ON DELETE CASCADE,
  rows_ref text NOT NULL REFERENCES waje_runtime.rows_metadata_authority(rows_ref),
  payload jsonb NOT NULL CHECK (
    jsonb_typeof(payload) = 'object'
    AND NOT (payload #> '{record,result_payload}' ? 'rows')
  ),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.query_completeness_reports (
  record_ref text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  report_ref text NOT NULL,
  report_digest text NOT NULL,
  result_ref text NOT NULL REFERENCES waje_runtime.query_runs(result_ref) ON DELETE CASCADE,
  query_contract_ref text NOT NULL REFERENCES waje_runtime.query_contracts(query_contract_id) ON DELETE CASCADE,
  completeness_status text NOT NULL,
  analysis_readiness text NOT NULL,
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS query_completeness_reports_report_ref_idx
  ON waje_runtime.query_completeness_reports (report_ref, created_at DESC, record_ref DESC);

CREATE TABLE IF NOT EXISTS waje_runtime.capability_binding_authority (
  record_ref text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  binding_digest text NOT NULL,
  capability_id text NOT NULL,
  analysis_contract_id text NOT NULL REFERENCES waje_runtime.analysis_contracts(analysis_contract_id) ON DELETE CASCADE,
  claim_strength_taxonomy_version text NOT NULL,
  maximum_claim_strength_rank integer NOT NULL CHECK (maximum_claim_strength_rank >= 0),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE waje_runtime.query_execution_authority
  ADD COLUMN IF NOT EXISTS run_id text;
ALTER TABLE waje_runtime.query_completeness_reports
  ADD COLUMN IF NOT EXISTS run_id text;
ALTER TABLE waje_runtime.capability_binding_authority
  ADD COLUMN IF NOT EXISTS run_id text;

UPDATE waje_runtime.query_execution_authority authority
SET run_id = run.run_id
FROM waje_runtime.query_runs run
WHERE authority.result_ref = run.result_ref
  AND authority.run_id IS NULL;
UPDATE waje_runtime.query_completeness_reports report
SET run_id = run.run_id
FROM waje_runtime.query_runs run
WHERE report.result_ref = run.result_ref
  AND report.run_id IS NULL;
UPDATE waje_runtime.capability_binding_authority binding
SET run_id = contract.run_id
FROM waje_runtime.analysis_contracts contract
WHERE binding.analysis_contract_id = contract.analysis_contract_id
  AND binding.run_id IS NULL;

ALTER TABLE waje_runtime.query_execution_authority ALTER COLUMN run_id SET NOT NULL;
ALTER TABLE waje_runtime.query_completeness_reports ALTER COLUMN run_id SET NOT NULL;
ALTER TABLE waje_runtime.capability_binding_authority ALTER COLUMN run_id SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'query_execution_authority_run_id_fkey'
      AND conrelid = 'waje_runtime.query_execution_authority'::regclass
  ) THEN
    ALTER TABLE waje_runtime.query_execution_authority
      ADD CONSTRAINT query_execution_authority_run_id_fkey
      FOREIGN KEY (run_id) REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'query_completeness_reports_run_id_fkey'
      AND conrelid = 'waje_runtime.query_completeness_reports'::regclass
  ) THEN
    ALTER TABLE waje_runtime.query_completeness_reports
      ADD CONSTRAINT query_completeness_reports_run_id_fkey
      FOREIGN KEY (run_id) REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'capability_binding_authority_run_id_fkey'
      AND conrelid = 'waje_runtime.capability_binding_authority'::regclass
  ) THEN
    ALTER TABLE waje_runtime.capability_binding_authority
      ADD CONSTRAINT capability_binding_authority_run_id_fkey
      FOREIGN KEY (run_id) REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE;
  END IF;
END $$;

ALTER TABLE waje_runtime.context_manifests
  ADD COLUMN IF NOT EXISTS run_id text REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  ADD COLUMN IF NOT EXISTS topic_id text REFERENCES waje_runtime.conversation_topics(topic_id) ON DELETE CASCADE,
  ADD COLUMN IF NOT EXISTS manifest_digest text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS waje_runtime.claim_provenance_records (
  record_ref text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  record_digest text NOT NULL,
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.answer_package_artifacts (
  artifact_ref text PRIMARY KEY,
  run_id text NOT NULL UNIQUE REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  canonical_path text NOT NULL,
  payload_digest text NOT NULL CHECK (length(payload_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.verified_claims (
  claim_ref text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  context_manifest_ref text NOT NULL REFERENCES waje_runtime.context_manifests(manifest_id) ON DELETE CASCADE,
  provenance_record_ref text NOT NULL REFERENCES waje_runtime.claim_provenance_records(record_ref),
  claim_digest text NOT NULL,
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.evidence_manifests (
  evidence_ref text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  binding_record_ref text NOT NULL REFERENCES waje_runtime.capability_binding_authority(record_ref),
  context_manifest_ref text NOT NULL,
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.claim_evidence_links (
  claim_ref text NOT NULL REFERENCES waje_runtime.verified_claims(claim_ref) ON DELETE CASCADE,
  evidence_ref text NOT NULL REFERENCES waje_runtime.evidence_manifests(evidence_ref) ON DELETE CASCADE,
  context_manifest_ref text NOT NULL REFERENCES waje_runtime.context_manifests(manifest_id) ON DELETE CASCADE,
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  PRIMARY KEY (claim_ref, evidence_ref)
);

CREATE TABLE IF NOT EXISTS waje_runtime.analysis_runtime_publications (
  run_id text PRIMARY KEY REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  analysis_contract_id text NOT NULL REFERENCES waje_runtime.analysis_contracts(analysis_contract_id) ON DELETE CASCADE,
  topic_id text NOT NULL REFERENCES waje_runtime.conversation_topics(topic_id) ON DELETE CASCADE,
  bundle_digest text NOT NULL CHECK (length(bundle_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'claim_evidence_links_claim_ref_fkey'
      AND conrelid = 'waje_runtime.claim_evidence_links'::regclass
  ) THEN
    ALTER TABLE waje_runtime.claim_evidence_links
      ADD CONSTRAINT claim_evidence_links_claim_ref_fkey
      FOREIGN KEY (claim_ref) REFERENCES waje_runtime.verified_claims(claim_ref) ON DELETE CASCADE;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'claim_evidence_links_context_manifest_ref_fkey'
      AND conrelid = 'waje_runtime.claim_evidence_links'::regclass
  ) THEN
    ALTER TABLE waje_runtime.claim_evidence_links
      ADD CONSTRAINT claim_evidence_links_context_manifest_ref_fkey
      FOREIGN KEY (context_manifest_ref) REFERENCES waje_runtime.context_manifests(manifest_id);
  END IF;
END $$;

ALTER TABLE waje_runtime.verified_claims
  DROP CONSTRAINT IF EXISTS verified_claims_context_manifest_ref_fkey;
ALTER TABLE waje_runtime.verified_claims
  ADD CONSTRAINT verified_claims_context_manifest_ref_fkey
  FOREIGN KEY (context_manifest_ref)
  REFERENCES waje_runtime.context_manifests(manifest_id) ON DELETE CASCADE;
ALTER TABLE waje_runtime.claim_evidence_links
  DROP CONSTRAINT IF EXISTS claim_evidence_links_context_manifest_ref_fkey;
ALTER TABLE waje_runtime.claim_evidence_links
  ADD CONSTRAINT claim_evidence_links_context_manifest_ref_fkey
  FOREIGN KEY (context_manifest_ref)
  REFERENCES waje_runtime.context_manifests(manifest_id) ON DELETE CASCADE;

CREATE TABLE IF NOT EXISTS waje_runtime.query_repair_attempts (
  attempt_ref text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  failed_signature text NOT NULL,
  action text NOT NULL,
  reason text NOT NULL,
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_analysis_contracts_run
  ON waje_runtime.analysis_contracts(run_id);
CREATE INDEX IF NOT EXISTS idx_query_contracts_run
  ON waje_runtime.query_contracts(run_id, analysis_contract_id);
CREATE INDEX IF NOT EXISTS idx_query_runs_run
  ON waje_runtime.query_runs(run_id, query_contract_id);
CREATE INDEX IF NOT EXISTS idx_evidence_manifests_run
  ON waje_runtime.evidence_manifests(run_id, binding_record_ref);
CREATE INDEX IF NOT EXISTS idx_claim_evidence_links_evidence
  ON waje_runtime.claim_evidence_links(evidence_ref);
CREATE INDEX IF NOT EXISTS idx_query_repair_attempts_run
  ON waje_runtime.query_repair_attempts(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_verified_claims_run
  ON waje_runtime.verified_claims(run_id, context_manifest_ref);
CREATE INDEX IF NOT EXISTS idx_claim_provenance_records_run
  ON waje_runtime.claim_provenance_records(run_id);
CREATE INDEX IF NOT EXISTS idx_answer_package_artifacts_run
  ON waje_runtime.answer_package_artifacts(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_contracts_run_identity
  ON waje_runtime.analysis_contracts(run_id, analysis_contract_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_query_contracts_run_identity
  ON waje_runtime.query_contracts(run_id, query_contract_id);
DROP INDEX IF EXISTS waje_runtime.idx_query_execution_authority_rows_ref;

CREATE INDEX IF NOT EXISTS idx_conversation_topics_thread ON waje_runtime.conversation_topics(thread_id);
CREATE INDEX IF NOT EXISTS idx_conversation_turns_thread ON waje_runtime.conversation_turns(thread_id);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_thread ON waje_runtime.analysis_runs(thread_id);
CREATE INDEX IF NOT EXISTS idx_result_refs_topic ON waje_runtime.result_refs(topic_id);
CREATE INDEX IF NOT EXISTS idx_analysis_assets_topic ON waje_runtime.analysis_assets(topic_id, created_at);
CREATE INDEX IF NOT EXISTS idx_investigation_artifacts_topic ON waje_runtime.investigation_artifacts(topic_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_events_thread ON waje_runtime.audit_events(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dataset_snapshots_lookup
  ON waje_runtime.dataset_snapshots(dataset_id, status, loaded_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dataset_snapshot_releases_identity
  ON waje_runtime.dataset_snapshot_releases(logical_snapshot_id, load_revision);
