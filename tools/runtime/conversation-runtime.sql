CREATE SCHEMA IF NOT EXISTS waje_runtime;

CREATE TABLE IF NOT EXISTS waje_runtime.investigation_threads (
  thread_id text PRIMARY KEY,
  owner_id text NOT NULL,
  current_topic_id text,
  pending_clarification_topic_id text,
  pending_clarification_id text NOT NULL DEFAULT '',
  state_version bigint NOT NULL DEFAULT 0 CHECK (state_version >= 0),
  active_task_id text,
  active_topic_ref text,
  pending_action_ref text,
  latest_item_sequence bigint NOT NULL DEFAULT 0
    CHECK (latest_item_sequence >= 0),
  customer_state text NOT NULL DEFAULT 'idle'
    CHECK (customer_state IN (
      'idle',
      'working',
      'needs_input',
      'completed',
      'completed_with_limits',
      'failed'
    )),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE waje_runtime.investigation_threads
  ADD COLUMN IF NOT EXISTS state_version bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS active_task_id text,
  ADD COLUMN IF NOT EXISTS active_topic_ref text,
  ADD COLUMN IF NOT EXISTS pending_action_ref text,
  ADD COLUMN IF NOT EXISTS latest_item_sequence bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS customer_state text NOT NULL DEFAULT 'idle';

ALTER TABLE waje_runtime.investigation_threads
  DROP CONSTRAINT IF EXISTS investigation_threads_state_version_check,
  DROP CONSTRAINT IF EXISTS investigation_threads_latest_item_sequence_check,
  DROP CONSTRAINT IF EXISTS investigation_threads_customer_state_check;

ALTER TABLE waje_runtime.investigation_threads
  ADD CONSTRAINT investigation_threads_state_version_check
    CHECK (state_version >= 0) NOT VALID,
  ADD CONSTRAINT investigation_threads_latest_item_sequence_check
    CHECK (latest_item_sequence >= 0) NOT VALID,
  ADD CONSTRAINT investigation_threads_customer_state_check
    CHECK (customer_state IN (
      'idle',
      'working',
      'needs_input',
      'completed',
      'completed_with_limits',
      'failed'
    )) NOT VALID;

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
  item_sequence bigint,
  item_type text NOT NULL DEFAULT 'message',
  operation_key text,
  item_digest text NOT NULL DEFAULT '',
  customer_visible boolean NOT NULL DEFAULT true,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE waje_runtime.conversation_messages
  ADD COLUMN IF NOT EXISTS item_sequence bigint,
  ADD COLUMN IF NOT EXISTS item_type text NOT NULL DEFAULT 'message',
  ADD COLUMN IF NOT EXISTS operation_key text,
  ADD COLUMN IF NOT EXISTS item_digest text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS customer_visible boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb;

WITH sequenced AS (
  SELECT message_id,
         row_number() OVER (
           PARTITION BY thread_id ORDER BY created_at, message_id
         ) AS item_sequence
  FROM waje_runtime.conversation_messages
  WHERE item_sequence IS NULL
)
UPDATE waje_runtime.conversation_messages message
SET item_sequence = sequenced.item_sequence
FROM sequenced
WHERE message.message_id = sequenced.message_id;

UPDATE waje_runtime.investigation_threads thread
SET latest_item_sequence = GREATEST(
      thread.latest_item_sequence,
      COALESCE(items.latest_item_sequence, 0)
    ),
    active_topic_ref = COALESCE(thread.active_topic_ref, thread.current_topic_id)
FROM (
  SELECT thread_id, max(item_sequence) AS latest_item_sequence
  FROM waje_runtime.conversation_messages
  GROUP BY thread_id
) items
WHERE thread.thread_id = items.thread_id;

ALTER TABLE waje_runtime.conversation_messages
  ALTER COLUMN item_sequence SET NOT NULL;

ALTER TABLE waje_runtime.conversation_messages
  DROP CONSTRAINT IF EXISTS conversation_messages_item_type_check,
  DROP CONSTRAINT IF EXISTS conversation_messages_item_digest_check,
  DROP CONSTRAINT IF EXISTS conversation_messages_payload_check;

ALTER TABLE waje_runtime.conversation_messages
  ADD CONSTRAINT conversation_messages_item_type_check CHECK (
    item_type IN (
      'message',
      'user_message',
      'assistant_message',
      'progress',
      'tool_call',
      'tool_result',
      'tool_selection',
      'clarification',
      'approval_request',
      'approval_decision',
      'artifact_reference',
      'task_terminal'
    )
  ) NOT VALID,
  ADD CONSTRAINT conversation_messages_item_digest_check CHECK (
    item_digest = '' OR length(item_digest) = 64
  ) NOT VALID,
  ADD CONSTRAINT conversation_messages_payload_check CHECK (
    jsonb_typeof(payload) = 'object'
  ) NOT VALID;

CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_messages_thread_sequence
  ON waje_runtime.conversation_messages(thread_id, item_sequence);

CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_messages_operation_key
  ON waje_runtime.conversation_messages(thread_id, operation_key)
  WHERE operation_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_conversation_messages_thread_recent
  ON waje_runtime.conversation_messages(thread_id, item_sequence DESC);

CREATE TABLE IF NOT EXISTS waje_runtime.agent_thread_summaries (
  summary_ref text PRIMARY KEY,
  thread_id text NOT NULL
    REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  summary_version integer NOT NULL CHECK (summary_version >= 1),
  covers_from_sequence bigint NOT NULL CHECK (covers_from_sequence = 1),
  covers_through_sequence bigint NOT NULL
    CHECK (covers_through_sequence >= covers_from_sequence),
  previous_summary_ref text
    REFERENCES waje_runtime.agent_thread_summaries(summary_ref),
  source_digest text NOT NULL,
  content_digest text NOT NULL,
  summary_digest text NOT NULL,
  summary_payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(thread_id, summary_version),
  UNIQUE(thread_id, covers_through_sequence)
);

CREATE INDEX IF NOT EXISTS idx_agent_thread_summaries_latest
  ON waje_runtime.agent_thread_summaries(thread_id, summary_version DESC);

CREATE TABLE IF NOT EXISTS waje_runtime.agent_generated_artifacts (
  artifact_ref text NOT NULL,
  thread_id text NOT NULL
    REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  operation_id text NOT NULL,
  artifact_type text NOT NULL
    CHECK (artifact_type = 'controlled_subagent_result'),
  artifact_version text NOT NULL,
  content_digest text NOT NULL,
  source_refs jsonb NOT NULL CHECK (jsonb_typeof(source_refs) = 'array'),
  visibility_policy_ref text NOT NULL
    CHECK (visibility_policy_ref = 'visibility:customer-safe'),
  customer_summary text NOT NULL,
  detail jsonb NOT NULL CHECK (jsonb_typeof(detail) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(thread_id, artifact_ref),
  UNIQUE(thread_id, operation_id, content_digest)
);

CREATE INDEX IF NOT EXISTS idx_agent_generated_artifacts_thread_recent
  ON waje_runtime.agent_generated_artifacts(thread_id, created_at DESC, artifact_ref);

CREATE OR REPLACE FUNCTION waje_runtime.allocate_thread_item_sequence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.item_sequence IS NULL THEN
    UPDATE waje_runtime.investigation_threads
    SET latest_item_sequence = latest_item_sequence + 1,
        state_version = state_version + 1,
        updated_at = now()
    WHERE thread_id = NEW.thread_id
    RETURNING latest_item_sequence INTO NEW.item_sequence;

    IF NEW.item_sequence IS NULL THEN
      RAISE EXCEPTION 'thread_item_thread_missing';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS conversation_messages_allocate_sequence
  ON waje_runtime.conversation_messages;
CREATE TRIGGER conversation_messages_allocate_sequence
  BEFORE INSERT ON waje_runtime.conversation_messages
  FOR EACH ROW
  EXECUTE FUNCTION waje_runtime.allocate_thread_item_sequence();

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
    CHECK (producer_kind IN ('thread_message', 'clarification_resolution')),
  scope_ref text NOT NULL,
  request_identity text NOT NULL,
  request_digest text NOT NULL
    CONSTRAINT run_dispatch_request_digest_check
    CHECK (length(request_digest) = 64),
  request_payload jsonb NOT NULL
    CONSTRAINT run_dispatch_request_payload_check
    CHECK (jsonb_typeof(request_payload) = 'object'),
  thread_id text NOT NULL REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
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
  CONSTRAINT run_dispatch_scope_shape_check CHECK (
    (producer_kind = 'thread_message' AND scope_ref = thread_id)
    OR (producer_kind = 'clarification_resolution' AND scope_ref = run_id)
  ),
  CONSTRAINT run_dispatch_owner_shape_check CHECK (
    dispatch_state NOT IN ('leased', 'running')
    OR (owner_id IS NOT NULL AND lease_expires_at IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS waje_runtime.agent_task_resume_outbox (
  resume_ref text PRIMARY KEY,
  thread_id text NOT NULL
    REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  task_ref text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  outbox_state text NOT NULL DEFAULT 'pending'
    CHECK (outbox_state IN (
      'pending', 'processing', 'completed', 'failed', 'exhausted'
    )),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  lease_owner_id text,
  lease_epoch bigint NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
  lease_expires_at timestamptz,
  last_error_code text,
  exhausted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(thread_id, task_ref)
);

ALTER TABLE waje_runtime.agent_task_resume_outbox
  ADD COLUMN IF NOT EXISTS lease_owner_id text,
  ADD COLUMN IF NOT EXISTS lease_epoch bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS exhausted_at timestamptz;

ALTER TABLE waje_runtime.agent_task_resume_outbox
  DROP CONSTRAINT IF EXISTS agent_task_resume_outbox_outbox_state_check;

ALTER TABLE waje_runtime.agent_task_resume_outbox
  ADD CONSTRAINT agent_task_resume_outbox_outbox_state_check
  CHECK (outbox_state IN (
    'pending', 'processing', 'completed', 'failed', 'exhausted'
  ));

DROP INDEX IF EXISTS waje_runtime.idx_agent_task_resume_outbox_ready;
CREATE INDEX idx_agent_task_resume_outbox_ready
  ON waje_runtime.agent_task_resume_outbox(
    outbox_state, lease_expires_at, updated_at, resume_ref
  )
  WHERE outbox_state IN ('pending', 'failed', 'processing');

CREATE INDEX IF NOT EXISTS idx_run_dispatch_recovery
  ON waje_runtime.run_dispatches(dispatch_state, lease_expires_at)
  WHERE dispatch_state IN ('pending', 'leased', 'running');

ALTER TABLE waje_runtime.run_dispatches
  DROP CONSTRAINT IF EXISTS run_dispatches_run_id_key;

ALTER TABLE waje_runtime.run_dispatches
  ALTER COLUMN message_id SET NOT NULL;

ALTER TABLE waje_runtime.run_dispatches
  DROP CONSTRAINT IF EXISTS run_dispatch_producer_kind_check;

ALTER TABLE waje_runtime.run_dispatches
  ADD CONSTRAINT run_dispatch_producer_kind_check
  CHECK (producer_kind IN ('thread_message', 'clarification_resolution'))
  NOT VALID;

ALTER TABLE waje_runtime.run_dispatches
  VALIDATE CONSTRAINT run_dispatch_producer_kind_check;

ALTER TABLE waje_runtime.run_dispatches
  DROP CONSTRAINT IF EXISTS run_dispatch_request_digest_check;

ALTER TABLE waje_runtime.run_dispatches
  ADD CONSTRAINT run_dispatch_request_digest_check
  CHECK (length(request_digest) = 64)
  NOT VALID;

ALTER TABLE waje_runtime.run_dispatches
  VALIDATE CONSTRAINT run_dispatch_request_digest_check;

ALTER TABLE waje_runtime.run_dispatches
  DROP CONSTRAINT IF EXISTS run_dispatch_request_payload_check;

ALTER TABLE waje_runtime.run_dispatches
  ADD CONSTRAINT run_dispatch_request_payload_check
  CHECK (jsonb_typeof(request_payload) = 'object')
  NOT VALID;

ALTER TABLE waje_runtime.run_dispatches
  VALIDATE CONSTRAINT run_dispatch_request_payload_check;

ALTER TABLE waje_runtime.run_dispatches
  DROP CONSTRAINT IF EXISTS run_dispatch_scope_shape_check;

ALTER TABLE waje_runtime.run_dispatches
  ADD CONSTRAINT run_dispatch_scope_shape_check
  CHECK (
    (producer_kind = 'thread_message' AND scope_ref = thread_id)
    OR (producer_kind = 'clarification_resolution' AND scope_ref = run_id)
  )
  NOT VALID;

ALTER TABLE waje_runtime.run_dispatches
  VALIDATE CONSTRAINT run_dispatch_scope_shape_check;

CREATE UNIQUE INDEX IF NOT EXISTS idx_run_dispatch_one_active_per_run
  ON waje_runtime.run_dispatches(run_id)
  WHERE dispatch_state IN ('pending', 'leased', 'running');

CREATE OR REPLACE FUNCTION waje_runtime.enforce_run_dispatch_command_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.dispatch_id IS DISTINCT FROM OLD.dispatch_id
     OR NEW.producer_kind IS DISTINCT FROM OLD.producer_kind
     OR NEW.scope_ref IS DISTINCT FROM OLD.scope_ref
     OR NEW.request_identity IS DISTINCT FROM OLD.request_identity
     OR NEW.request_digest IS DISTINCT FROM OLD.request_digest
     OR NEW.request_payload IS DISTINCT FROM OLD.request_payload
     OR NEW.thread_id IS DISTINCT FROM OLD.thread_id
     OR NEW.run_id IS DISTINCT FROM OLD.run_id
     OR NEW.message_id IS DISTINCT FROM OLD.message_id
     OR NEW.created_at IS DISTINCT FROM OLD.created_at
  THEN
    RAISE EXCEPTION 'run_dispatch_command_immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS run_dispatch_command_immutable
  ON waje_runtime.run_dispatches;
CREATE TRIGGER run_dispatch_command_immutable
  BEFORE UPDATE ON waje_runtime.run_dispatches
  FOR EACH ROW
  EXECUTE FUNCTION waje_runtime.enforce_run_dispatch_command_immutable();

COMMENT ON TABLE waje_runtime.run_dispatches IS
  'One immutable user-command envelope per dispatch; lifecycle lease fields remain mutable.';
COMMENT ON COLUMN waje_runtime.run_dispatches.request_digest IS
  'SHA-256 of canonical JSON {producer_kind,scope_ref,thread_id,request_payload}; request_identity belongs to the separate idempotency tuple.';

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

CREATE TABLE IF NOT EXISTS waje_runtime.memory_items (
  memory_id text PRIMARY KEY,
  owner_id text NOT NULL,
  text text NOT NULL,
  source_ref text NOT NULL,
  status text NOT NULL,
  ttl text NOT NULL DEFAULT 'until_revoked',
  confidence text NOT NULL DEFAULT 'user_confirmed',
  refresh_rule text NOT NULL DEFAULT 'refresh_on_contract_or_owner_change',
  revocation_path text NOT NULL DEFAULT 'memory_proposal_revoke_or_admin_action',
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz
);

ALTER TABLE waje_runtime.memory_items
  ADD COLUMN IF NOT EXISTS refresh_rule text NOT NULL DEFAULT 'refresh_on_contract_or_owner_change';

ALTER TABLE waje_runtime.memory_items
  ADD COLUMN IF NOT EXISTS revocation_path text NOT NULL DEFAULT 'memory_proposal_revoke_or_admin_action';

CREATE TABLE IF NOT EXISTS waje_runtime.memory_proposals (
  proposal_id text PRIMARY KEY,
  thread_id text NOT NULL REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE CASCADE,
  text text NOT NULL,
  source_ref text NOT NULL,
  owner_id text NOT NULL,
  status text NOT NULL DEFAULT 'proposed',
  created_at timestamptz NOT NULL DEFAULT now(),
  decided_at timestamptz
);

CREATE TABLE IF NOT EXISTS waje_runtime.audit_events (
  audit_id bigserial PRIMARY KEY,
  event_type text NOT NULL,
  actor_id text NOT NULL DEFAULT '',
  thread_id text REFERENCES waje_runtime.investigation_threads(thread_id)
    ON DELETE CASCADE,
  topic_id text,
  run_id text,
  ref text,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_events_general_agent_trace
  ON waje_runtime.audit_events(run_id, created_at DESC, audit_id DESC)
  WHERE event_type = 'agents_sdk_trace_recorded';

DROP TRIGGER IF EXISTS investigation_threads_delete_agent_traces
  ON waje_runtime.investigation_threads;
DROP FUNCTION IF EXISTS waje_runtime.delete_agent_traces_for_thread();

ALTER TABLE waje_runtime.audit_events
  DROP CONSTRAINT IF EXISTS audit_events_thread_id_fkey;

ALTER TABLE waje_runtime.audit_events
  ADD CONSTRAINT audit_events_thread_id_fkey
  FOREIGN KEY (thread_id)
  REFERENCES waje_runtime.investigation_threads(thread_id)
  ON DELETE CASCADE
  NOT VALID;

CREATE TABLE IF NOT EXISTS waje_runtime.dataset_snapshots (
  snapshot_ref text PRIMARY KEY,
  dataset_id text NOT NULL,
  physical_table text NOT NULL,
  watermark date NOT NULL,
  schema_fingerprint text NOT NULL,
  schema_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
  contract_ref text NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_analysis_contracts_run
  ON waje_runtime.analysis_contracts(run_id);
CREATE INDEX IF NOT EXISTS idx_query_contracts_run
  ON waje_runtime.query_contracts(run_id, analysis_contract_id);
CREATE INDEX IF NOT EXISTS idx_query_runs_run
  ON waje_runtime.query_runs(run_id, query_contract_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_contracts_run_identity
  ON waje_runtime.analysis_contracts(run_id, analysis_contract_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_query_contracts_run_identity
  ON waje_runtime.query_contracts(run_id, query_contract_id);
DROP INDEX IF EXISTS waje_runtime.idx_query_execution_authority_rows_ref;

CREATE INDEX IF NOT EXISTS idx_conversation_topics_thread ON waje_runtime.conversation_topics(thread_id);
CREATE INDEX IF NOT EXISTS idx_conversation_turns_thread ON waje_runtime.conversation_turns(thread_id);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_thread ON waje_runtime.analysis_runs(thread_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_thread ON waje_runtime.audit_events(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dataset_snapshots_lookup
  ON waje_runtime.dataset_snapshots(dataset_id, status, loaded_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dataset_snapshot_releases_identity
  ON waje_runtime.dataset_snapshot_releases(logical_snapshot_id, load_revision);

-- Current single-authority workflow slice. These tables are the durable
-- authority for intent, planning, provider calls, capability execution,
-- evidence, claims, narrative, publication, and delivery. run_nodes remains a
-- business-process projection.
CREATE TABLE IF NOT EXISTS waje_runtime.schema_migrations (
  migration_id text PRIMARY KEY,
  migration_digest text NOT NULL CHECK (length(migration_digest) = 64),
  applied_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE waje_runtime.analysis_runs
  ADD COLUMN IF NOT EXISTS run_attempt_id text,
  ADD COLUMN IF NOT EXISTS intent_revision_id text;

UPDATE waje_runtime.analysis_runs
SET run_attempt_id = run_id
WHERE run_attempt_id IS NULL OR run_attempt_id = '';

ALTER TABLE waje_runtime.analysis_runs
  ALTER COLUMN run_attempt_id SET NOT NULL;

CREATE TABLE IF NOT EXISTS waje_runtime.intent_revisions (
  intent_revision_id text PRIMARY KEY,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  supersedes_intent_revision_id text REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  original_user_text text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  material_binding_digest text NOT NULL CHECK (length(material_binding_digest) = 64),
  schema_version text NOT NULL,
  prompt_version text NOT NULL,
  model_version text NOT NULL,
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(run_attempt_id, content_digest),
  CHECK (supersedes_intent_revision_id IS NULL OR supersedes_intent_revision_id <> intent_revision_id)
);

ALTER TABLE waje_runtime.intent_revisions
  DROP CONSTRAINT IF EXISTS intent_revisions_run_attempt_id_key;

CREATE TABLE IF NOT EXISTS waje_runtime.intent_revision_supersessions (
  supersession_id text PRIMARY KEY,
  superseded_intent_revision_id text NOT NULL UNIQUE
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  successor_intent_revision_id text NOT NULL UNIQUE
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  affected_plan_fields jsonb NOT NULL CHECK (jsonb_typeof(affected_plan_fields) = 'array'),
  reason_ref text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (superseded_intent_revision_id <> successor_intent_revision_id)
);

CREATE TABLE IF NOT EXISTS waje_runtime.decision_options (
  intent_revision_id text NOT NULL
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  slot_id text NOT NULL,
  option_id text NOT NULL,
  typed_value jsonb NOT NULL,
  display_label text NOT NULL,
  display_description text NOT NULL DEFAULT '',
  recommended boolean NOT NULL DEFAULT false,
  display_position integer NOT NULL CHECK (display_position > 0),
  option_set_digest text NOT NULL CHECK (length(option_set_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (intent_revision_id, slot_id, option_id)
);

ALTER TABLE waje_runtime.decision_options
  ADD COLUMN IF NOT EXISTS display_position integer NOT NULL DEFAULT 1;
ALTER TABLE waje_runtime.decision_options
  ALTER COLUMN display_position DROP DEFAULT;

CREATE TABLE IF NOT EXISTS waje_runtime.decision_records (
  ledger_position bigint NOT NULL,
  decision_id text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  intent_revision_id text NOT NULL
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  slot_id text NOT NULL,
  option_id text,
  source text NOT NULL CHECK (source IN (
    'user', 'accepted_recommendation', 'safe_inference', 'inherited', 'system'
  )),
  status text NOT NULL CHECK (status IN (
    'unresolved', 'inferred', 'user_confirmed', 'invalidated'
  )),
  materiality text NOT NULL CHECK (materiality IN ('material', 'non_material')),
  invalidated_by_revision_id text
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  supersedes_decision_id text
    REFERENCES waje_runtime.decision_records(decision_id) ON DELETE RESTRICT,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (
    (status = 'invalidated' AND invalidated_by_revision_id IS NOT NULL AND supersedes_decision_id IS NOT NULL)
    OR (status <> 'invalidated' AND invalidated_by_revision_id IS NULL)
  )
);

ALTER TABLE waje_runtime.decision_records
  ADD COLUMN IF NOT EXISTS run_attempt_id text;

UPDATE waje_runtime.decision_records record
SET run_attempt_id = revision.run_attempt_id
FROM waje_runtime.intent_revisions revision
WHERE record.intent_revision_id = revision.intent_revision_id
  AND (record.run_attempt_id IS NULL OR record.run_attempt_id = '');

ALTER TABLE waje_runtime.decision_records
  ALTER COLUMN run_attempt_id SET NOT NULL,
  ALTER COLUMN ledger_position DROP DEFAULT,
  DROP CONSTRAINT IF EXISTS decision_records_ledger_position_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_records_ledger_position
  ON waje_runtime.decision_records(run_attempt_id, ledger_position);

CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_records_option_idempotency
  ON waje_runtime.decision_records(intent_revision_id, slot_id, option_id)
  WHERE option_id IS NOT NULL AND status <> 'invalidated';
CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_records_content_idempotency
  ON waje_runtime.decision_records(intent_revision_id, slot_id, content_digest);

CREATE TABLE IF NOT EXISTS waje_runtime.workflow_transition_attempts (
  attempt_id text PRIMARY KEY,
  transition_id text NOT NULL,
  node_name text NOT NULL,
  parent_transition_id text,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  intent_revision_id text
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  decision_ledger_position bigint NOT NULL DEFAULT 0 CHECK (decision_ledger_position >= 0),
  input_digest text NOT NULL CHECK (length(input_digest) = 64),
  output_digest text CHECK (output_digest IS NULL OR length(output_digest) = 64),
  execution_attempt integer NOT NULL CHECK (execution_attempt > 0),
  provider_ref text NOT NULL,
  model_ref text NOT NULL,
  status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
  acceptance_state text NOT NULL CHECK (acceptance_state IN (
    'pending', 'accepted', 'rejected', 'orphaned'
  )),
  next_transition text NOT NULL,
  input_payload jsonb NOT NULL CHECK (jsonb_typeof(input_payload) = 'object'),
  output_payload jsonb CHECK (output_payload IS NULL OR jsonb_typeof(output_payload) = 'object'),
  failure_ref text,
  started_at timestamptz NOT NULL,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(transition_id, execution_attempt),
  CHECK (acceptance_state <> 'accepted' OR status = 'succeeded')
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_transition_one_accepted
  ON waje_runtime.workflow_transition_attempts(transition_id)
  WHERE acceptance_state = 'accepted';
CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_transition_resume_once
  ON waje_runtime.workflow_transition_attempts(
    run_attempt_id, node_name, input_digest
  )
  WHERE acceptance_state = 'accepted';
CREATE INDEX IF NOT EXISTS idx_workflow_transition_resume
  ON waje_runtime.workflow_transition_attempts(
    run_attempt_id, node_name, input_digest, acceptance_state, execution_attempt DESC
  );

CREATE TABLE IF NOT EXISTS waje_runtime.failure_records (
  failure_id text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  layer text NOT NULL,
  kind text NOT NULL,
  scope text NOT NULL,
  affected_refs jsonb NOT NULL CHECK (jsonb_typeof(affected_refs) = 'array'),
  integrity_level text NOT NULL,
  retryability text NOT NULL,
  user_actionable boolean NOT NULL,
  business_boundary text NOT NULL,
  technical_detail_ref text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_attempt_id, failure_id)
);

CREATE TABLE IF NOT EXISTS waje_runtime.interaction_directives (
  directive_id text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  intent_revision_id text NOT NULL
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  kind text NOT NULL CHECK (
    kind IN ('material_intent_change', 'cancel', 'challenge')
  ),
  target_refs jsonb NOT NULL CHECK (jsonb_typeof(target_refs) = 'array'),
  original_user_text text NOT NULL,
  source text NOT NULL CHECK (source = 'user'),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(run_attempt_id, content_digest)
);

CREATE TABLE IF NOT EXISTS waje_runtime.run_lifecycle_state_revisions (
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  state_revision integer NOT NULL CHECK (state_revision > 0),
  execution_state text NOT NULL,
  interaction_state text NOT NULL,
  evidence_state text NOT NULL,
  publication_state text NOT NULL,
  delivery_state text NOT NULL,
  retry_state text NOT NULL,
  cancellation_state text NOT NULL,
  supersession_state text NOT NULL,
  prior_state_digest text CHECK (prior_state_digest IS NULL OR length(prior_state_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_attempt_id, state_revision),
  UNIQUE(run_attempt_id, content_digest)
);

CREATE TABLE IF NOT EXISTS waje_runtime.orphaned_results (
  orphaned_result_id text PRIMARY KEY,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  result_intent_revision_id text NOT NULL
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  active_intent_revision_id text
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  source_transition_id text NOT NULL,
  result_ref text NOT NULL,
  reason text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(run_attempt_id, result_ref, content_digest)
);

CREATE OR REPLACE FUNCTION waje_runtime.reject_append_only_authority_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'append_only_authority_record:%', TG_TABLE_NAME;
END;
$$;

DROP TRIGGER IF EXISTS intent_revisions_append_only ON waje_runtime.intent_revisions;
CREATE TRIGGER intent_revisions_append_only
  BEFORE UPDATE OR DELETE ON waje_runtime.intent_revisions
  FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation();
DROP TRIGGER IF EXISTS intent_revision_supersessions_append_only ON waje_runtime.intent_revision_supersessions;
CREATE TRIGGER intent_revision_supersessions_append_only
  BEFORE UPDATE OR DELETE ON waje_runtime.intent_revision_supersessions
  FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation();
DROP TRIGGER IF EXISTS decision_options_append_only ON waje_runtime.decision_options;
CREATE TRIGGER decision_options_append_only
  BEFORE UPDATE OR DELETE ON waje_runtime.decision_options
  FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation();
DROP TRIGGER IF EXISTS decision_records_append_only ON waje_runtime.decision_records;
CREATE TRIGGER decision_records_append_only
  BEFORE UPDATE OR DELETE ON waje_runtime.decision_records
  FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation();
DROP TRIGGER IF EXISTS failure_records_append_only ON waje_runtime.failure_records;
CREATE TRIGGER failure_records_append_only
  BEFORE UPDATE OR DELETE ON waje_runtime.failure_records
  FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation();
DROP TRIGGER IF EXISTS interaction_directives_append_only ON waje_runtime.interaction_directives;
CREATE TRIGGER interaction_directives_append_only
  BEFORE UPDATE OR DELETE ON waje_runtime.interaction_directives
  FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation();
DROP TRIGGER IF EXISTS run_lifecycle_state_revisions_append_only ON waje_runtime.run_lifecycle_state_revisions;
CREATE TRIGGER run_lifecycle_state_revisions_append_only
  BEFORE UPDATE OR DELETE ON waje_runtime.run_lifecycle_state_revisions
  FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation();
DROP TRIGGER IF EXISTS orphaned_results_append_only ON waje_runtime.orphaned_results;
CREATE TRIGGER orphaned_results_append_only
  BEFORE UPDATE OR DELETE ON waje_runtime.orphaned_results
  FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation();

-- vNext Phase 2 immutable planning authority. Active state is derived from the
-- append-only supersession relation; no mutable plan status is stored.
CREATE TABLE IF NOT EXISTS waje_runtime.authority_contexts (
  authority_context_ref text PRIMARY KEY,
  run_attempt_id text NOT NULL UNIQUE
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  actual_as_of timestamptz NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(run_attempt_id, content_digest)
);

CREATE TABLE IF NOT EXISTS waje_runtime.planner_proposals (
  planner_proposal_id text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  intent_revision_id text NOT NULL
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  authority_context_ref text NOT NULL
    REFERENCES waje_runtime.authority_contexts(authority_context_ref) ON DELETE RESTRICT,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  schema_version text NOT NULL,
  prompt_version text NOT NULL,
  model_version text NOT NULL,
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(run_attempt_id, content_digest)
);

CREATE TABLE IF NOT EXISTS waje_runtime.proposal_admission_records (
  proposal_admission_id text PRIMARY KEY,
  planner_proposal_ref text NOT NULL
    REFERENCES waje_runtime.planner_proposals(planner_proposal_id) ON DELETE RESTRICT,
  intent_revision_id text NOT NULL
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  authority_context_ref text NOT NULL
    REFERENCES waje_runtime.authority_contexts(authority_context_ref) ON DELETE RESTRICT,
  compiler_version text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(planner_proposal_ref, content_digest)
);

CREATE TABLE IF NOT EXISTS waje_runtime.plan_revisions (
  plan_revision_id text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  intent_revision_id text NOT NULL
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  authority_context_ref text NOT NULL
    REFERENCES waje_runtime.authority_contexts(authority_context_ref) ON DELETE RESTRICT,
  planner_proposal_ref text NOT NULL
    REFERENCES waje_runtime.planner_proposals(planner_proposal_id) ON DELETE RESTRICT,
  proposal_admission_ref text NOT NULL
    REFERENCES waje_runtime.proposal_admission_records(proposal_admission_id) ON DELETE RESTRICT,
  supersedes_plan_revision_id text
    REFERENCES waje_runtime.plan_revisions(plan_revision_id) ON DELETE RESTRICT,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(run_attempt_id, content_digest),
  CHECK (
    supersedes_plan_revision_id IS NULL
    OR supersedes_plan_revision_id <> plan_revision_id
  )
);

CREATE TABLE IF NOT EXISTS waje_runtime.plan_revision_supersessions (
  supersession_id text PRIMARY KEY,
  superseded_plan_revision_id text NOT NULL UNIQUE
    REFERENCES waje_runtime.plan_revisions(plan_revision_id) ON DELETE RESTRICT,
  successor_plan_revision_id text NOT NULL UNIQUE
    REFERENCES waje_runtime.plan_revisions(plan_revision_id) ON DELETE RESTRICT,
  authority_context_ref text NOT NULL
    REFERENCES waje_runtime.authority_contexts(authority_context_ref) ON DELETE RESTRICT,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (superseded_plan_revision_id <> successor_plan_revision_id)
);

DROP TRIGGER IF EXISTS authority_contexts_append_only ON waje_runtime.authority_contexts;
CREATE TRIGGER authority_contexts_append_only
  BEFORE UPDATE OR DELETE ON waje_runtime.authority_contexts
  FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation();
DROP TRIGGER IF EXISTS planner_proposals_append_only ON waje_runtime.planner_proposals;
CREATE TRIGGER planner_proposals_append_only
  BEFORE UPDATE OR DELETE ON waje_runtime.planner_proposals
  FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation();
DROP TRIGGER IF EXISTS proposal_admission_records_append_only ON waje_runtime.proposal_admission_records;
CREATE TRIGGER proposal_admission_records_append_only
  BEFORE UPDATE OR DELETE ON waje_runtime.proposal_admission_records
  FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation();
DROP TRIGGER IF EXISTS plan_revisions_append_only ON waje_runtime.plan_revisions;
CREATE TRIGGER plan_revisions_append_only
  BEFORE UPDATE OR DELETE ON waje_runtime.plan_revisions
  FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation();
DROP TRIGGER IF EXISTS plan_revision_supersessions_append_only ON waje_runtime.plan_revision_supersessions;
CREATE TRIGGER plan_revision_supersessions_append_only
  BEFORE UPDATE OR DELETE ON waje_runtime.plan_revision_supersessions
  FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation();

CREATE INDEX IF NOT EXISTS idx_planner_proposals_run
  ON waje_runtime.planner_proposals(run_attempt_id, created_at);
CREATE INDEX IF NOT EXISTS idx_plan_revisions_run
  ON waje_runtime.plan_revisions(run_attempt_id, created_at);

-- Every external provider or capability invocation is journaled before the
-- call starts. Attempts and events are append-only. A single acceptance row is
-- the compare-and-swap winner for one logical input, and stage bindings close
-- an accepted transition over those already-persisted winners.
CREATE TABLE IF NOT EXISTS waje_runtime.durable_call_attempts (
  attempt_ref text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  intent_revision_id text
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  plan_revision_id text
    REFERENCES waje_runtime.plan_revisions(plan_revision_id) ON DELETE RESTRICT,
  task_id text,
  stage_name text NOT NULL,
  call_kind text NOT NULL,
  operation_name text NOT NULL,
  input_ref text NOT NULL,
  input_digest text NOT NULL CHECK (length(input_digest) = 64),
  idempotency_key text NOT NULL CHECK (length(idempotency_key) = 64),
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  retry_reason text NOT NULL CHECK (
    retry_reason IN (
      'initial', 'previous_attempt_incomplete', 'previous_attempt_failed'
    )
  ),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(run_attempt_id, attempt_ref),
  UNIQUE(run_attempt_id, idempotency_key, attempt_number)
);

ALTER TABLE waje_runtime.durable_call_attempts
  DROP CONSTRAINT IF EXISTS durable_call_attempts_call_kind_check;
ALTER TABLE waje_runtime.durable_call_attempts
  DROP CONSTRAINT IF EXISTS durable_call_attempts_check;
ALTER TABLE waje_runtime.durable_call_attempts
  DROP CONSTRAINT IF EXISTS durable_call_attempts_scope_check;
ALTER TABLE waje_runtime.durable_call_attempts
  ADD CONSTRAINT durable_call_attempts_call_kind_check CHECK (call_kind IN (
    'conversation_provider', 'topic_selection', 'intent_provider',
    'clarification_provider',
    'planner_provider', 'plan_patch_provider', 'query', 'capability',
    'semantic_provider', 'controlled_investigation_provider',
    'narrative_provider'
  ));
ALTER TABLE waje_runtime.durable_call_attempts
  ADD CONSTRAINT durable_call_attempts_scope_check CHECK (
    (call_kind IN ('conversation_provider', 'topic_selection', 'intent_provider')
      AND intent_revision_id IS NULL
      AND plan_revision_id IS NULL
      AND task_id IS NULL)
    OR (call_kind IN ('clarification_provider', 'planner_provider')
      AND intent_revision_id IS NOT NULL
      AND plan_revision_id IS NULL
      AND task_id IS NULL)
    OR (call_kind IN (
        'plan_patch_provider', 'semantic_provider',
        'controlled_investigation_provider', 'narrative_provider'
      )
      AND intent_revision_id IS NOT NULL
      AND plan_revision_id IS NOT NULL
      AND task_id IS NULL)
    OR (call_kind IN ('query', 'capability')
      AND intent_revision_id IS NOT NULL
      AND plan_revision_id IS NOT NULL
      AND task_id IS NOT NULL)
  );

CREATE TABLE IF NOT EXISTS waje_runtime.durable_call_attempt_events (
  event_ref text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  attempt_ref text NOT NULL,
  event_sequence integer NOT NULL CHECK (event_sequence BETWEEN 1 AND 3),
  status text NOT NULL CHECK (status IN (
    'claimed', 'started', 'succeeded', 'failed'
  )),
  success_disposition text CHECK (
    success_disposition IN ('accepted', 'orphaned')
  ),
  output_digest text CHECK (
    output_digest IS NULL OR length(output_digest) = 64
  ),
  output_payload jsonb CHECK (
    output_payload IS NULL OR jsonb_typeof(output_payload) = 'object'
  ),
  failure_code text,
  failure_payload jsonb CHECK (
    failure_payload IS NULL OR jsonb_typeof(failure_payload) = 'object'
  ),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(attempt_ref, event_sequence),
  FOREIGN KEY (run_attempt_id, attempt_ref)
    REFERENCES waje_runtime.durable_call_attempts(
      run_attempt_id, attempt_ref
    ) ON DELETE RESTRICT,
  CHECK (
    (status = 'claimed' AND event_sequence = 1)
    OR (status = 'started' AND event_sequence = 2)
    OR (status IN ('succeeded', 'failed') AND event_sequence = 3)
  ),
  CHECK (
    (status = 'succeeded'
      AND success_disposition IN ('accepted', 'orphaned')
      AND output_digest IS NOT NULL
      AND output_payload IS NOT NULL AND failure_code IS NULL)
    OR (status = 'failed' AND output_digest IS NULL
      AND output_payload IS NULL AND failure_code IS NOT NULL
      AND success_disposition IS NULL)
    OR (status IN ('claimed', 'started') AND output_digest IS NULL
      AND output_payload IS NULL AND failure_code IS NULL
      AND failure_payload IS NULL AND success_disposition IS NULL)
  )
);

CREATE TABLE IF NOT EXISTS waje_runtime.durable_call_acceptances (
  acceptance_ref text NOT NULL UNIQUE,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  idempotency_key text NOT NULL CHECK (length(idempotency_key) = 64),
  accepted_attempt_ref text NOT NULL,
  output_digest text NOT NULL CHECK (length(output_digest) = 64),
  output_payload jsonb NOT NULL CHECK (jsonb_typeof(output_payload) = 'object'),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(run_attempt_id, idempotency_key),
  UNIQUE(run_attempt_id, accepted_attempt_ref),
  FOREIGN KEY (run_attempt_id, accepted_attempt_ref)
    REFERENCES waje_runtime.durable_call_attempts(
      run_attempt_id, attempt_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.durable_stage_attempt_seals (
  stage_seal_ref text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  transition_attempt_id text NOT NULL
    REFERENCES waje_runtime.workflow_transition_attempts(attempt_id)
      ON DELETE RESTRICT,
  stage_name text NOT NULL,
  attempt_set_digest text NOT NULL CHECK (length(attempt_set_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(run_attempt_id, stage_seal_ref),
  UNIQUE(run_attempt_id, transition_attempt_id, stage_name)
);

CREATE TABLE IF NOT EXISTS waje_runtime.durable_stage_attempt_bindings (
  binding_ref text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  stage_seal_ref text NOT NULL,
  transition_attempt_id text NOT NULL
    REFERENCES waje_runtime.workflow_transition_attempts(attempt_id)
      ON DELETE RESTRICT,
  stage_name text NOT NULL,
  accepted_attempt_ref text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(run_attempt_id, transition_attempt_id, accepted_attempt_ref),
  FOREIGN KEY (run_attempt_id, stage_seal_ref)
    REFERENCES waje_runtime.durable_stage_attempt_seals(
      run_attempt_id, stage_seal_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (run_attempt_id, accepted_attempt_ref)
    REFERENCES waje_runtime.durable_call_acceptances(
      run_attempt_id, accepted_attempt_ref
    ) ON DELETE RESTRICT
);

DO $$
DECLARE journal_table text;
BEGIN
  FOREACH journal_table IN ARRAY ARRAY[
    'durable_call_attempts',
    'durable_call_attempt_events',
    'durable_call_acceptances',
    'durable_stage_attempt_seals',
    'durable_stage_attempt_bindings'
  ]
  LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS %I_append_only ON waje_runtime.%I',
      journal_table,
      journal_table
    );
    EXECUTE format(
      'CREATE TRIGGER %I_append_only BEFORE UPDATE OR DELETE ON waje_runtime.%I '
      'FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation()',
      journal_table,
      journal_table
    );
  END LOOP;
END
$$;

CREATE INDEX IF NOT EXISTS idx_durable_call_attempts_logical_call
  ON waje_runtime.durable_call_attempts(
    run_attempt_id, idempotency_key, attempt_number DESC
  );
CREATE INDEX IF NOT EXISTS idx_durable_stage_attempt_bindings_transition
  ON waje_runtime.durable_stage_attempt_bindings(
    run_attempt_id, transition_attempt_id, stage_name
  );

-- vNext Phase 3 task-scoped execution authority. CapabilityTask definitions
-- remain embedded in the immutable PlanRevision. Dispatch leases are mutable
-- operational coordination only; attempts, outcomes, evidence ledger entries,
-- stop records, and settled snapshots are append-only authority.
CREATE TABLE IF NOT EXISTS waje_runtime.capability_task_attempts (
  attempt_id text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  intent_revision_id text NOT NULL
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  plan_revision_id text NOT NULL
    REFERENCES waje_runtime.plan_revisions(plan_revision_id) ON DELETE RESTRICT,
  task_id text NOT NULL,
  task_idempotency_key text NOT NULL CHECK (length(task_idempotency_key) = 64),
  execution_attempt integer NOT NULL CHECK (execution_attempt > 0),
  normalized_input_digest text NOT NULL CHECK (length(normalized_input_digest) = 64),
  release_set_digest text NOT NULL CHECK (length(release_set_digest) = 64),
  contract_versions_digest text NOT NULL CHECK (length(contract_versions_digest) = 64),
  input_digest text NOT NULL CHECK (length(input_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (run_attempt_id, attempt_id)
    REFERENCES waje_runtime.durable_call_attempts(
      run_attempt_id, attempt_ref
    ) ON DELETE RESTRICT,
  UNIQUE(plan_revision_id, task_id, execution_attempt),
  UNIQUE(task_idempotency_key, execution_attempt)
);

CREATE TABLE IF NOT EXISTS waje_runtime.capability_failure_records (
  failure_ref text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  plan_revision_id text NOT NULL
    REFERENCES waje_runtime.plan_revisions(plan_revision_id) ON DELETE RESTRICT,
  task_id text NOT NULL,
  attempt_id text NOT NULL
    REFERENCES waje_runtime.capability_task_attempts(attempt_id) ON DELETE RESTRICT,
  layer text NOT NULL CHECK (layer IN ('query', 'capability', 'evidence', 'persistence')),
  kind text NOT NULL,
  integrity_level text NOT NULL CHECK (integrity_level IN ('expected_boundary', 'task', 'shared_authority')),
  retryability text NOT NULL CHECK (retryability IN ('never', 'same_input', 'replan_required')),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(attempt_id, content_digest)
);

CREATE TABLE IF NOT EXISTS waje_runtime.capability_outcomes (
  outcome_ref text PRIMARY KEY,
  attempt_id text NOT NULL UNIQUE
    REFERENCES waje_runtime.capability_task_attempts(attempt_id) ON DELETE RESTRICT,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  plan_revision_id text NOT NULL
    REFERENCES waje_runtime.plan_revisions(plan_revision_id) ON DELETE RESTRICT,
  task_id text NOT NULL,
  status text NOT NULL CHECK (
    status IN (
      'succeeded', 'unavailable', 'integrity_failed',
      'technical_failed', 'skipped', 'superseded'
    )
  ),
  retryability text NOT NULL CHECK (retryability IN ('never', 'same_input', 'replan_required')),
  failure_ref text
    REFERENCES waje_runtime.capability_failure_records(failure_ref) ON DELETE RESTRICT,
  input_digest text NOT NULL CHECK (length(input_digest) = 64),
  output_digest text NOT NULL CHECK (length(output_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(plan_revision_id, task_id)
);

CREATE TABLE IF NOT EXISTS waje_runtime.capability_evidence_ledger_entries (
  entry_ref text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_context_ref text NOT NULL
    REFERENCES waje_runtime.authority_contexts(authority_context_ref) ON DELETE RESTRICT,
  plan_revision_id text NOT NULL
    REFERENCES waje_runtime.plan_revisions(plan_revision_id) ON DELETE RESTRICT,
  task_id text NOT NULL,
  outcome_ref text NOT NULL
    REFERENCES waje_runtime.capability_outcomes(outcome_ref) ON DELETE RESTRICT,
  evidence_ref text NOT NULL,
  binding_record_ref text
    REFERENCES waje_runtime.capability_binding_authority(record_ref) ON DELETE RESTRICT,
  execution_state text NOT NULL CHECK (
    execution_state IN ('available', 'unavailable', 'integrity_failed', 'technical_failed')
  ),
  evidence_kind text NOT NULL
    CONSTRAINT capability_evidence_ledger_entries_evidence_kind_check
    CHECK (
      evidence_kind IN ('boundary', 'observed', 'derived', 'scenario', 'statistical_association')
    ),
  data_contract_state text NOT NULL,
  maximum_claim_strength text NOT NULL,
  result_membership_digest text NOT NULL CHECK (length(result_membership_digest) = 64),
  completeness_membership_digest text NOT NULL CHECK (length(completeness_membership_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(outcome_ref, evidence_ref, content_digest)
);

ALTER TABLE waje_runtime.capability_evidence_ledger_entries
  DROP CONSTRAINT IF EXISTS capability_evidence_ledger_entries_evidence_kind_check;

ALTER TABLE waje_runtime.capability_evidence_ledger_entries
  ADD CONSTRAINT capability_evidence_ledger_entries_evidence_kind_check
  CHECK (
    evidence_kind IN ('boundary', 'observed', 'derived', 'scenario', 'statistical_association')
  ) NOT VALID;

ALTER TABLE waje_runtime.capability_evidence_ledger_entries
  VALIDATE CONSTRAINT capability_evidence_ledger_entries_evidence_kind_check;

CREATE TABLE IF NOT EXISTS waje_runtime.exploration_stop_records (
  stop_ref text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  plan_revision_id text NOT NULL UNIQUE
    REFERENCES waje_runtime.plan_revisions(plan_revision_id) ON DELETE RESTRICT,
  evaluated_outcome_set_digest text NOT NULL CHECK (length(evaluated_outcome_set_digest) = 64),
  budget_policy_ref text NOT NULL,
  reason text NOT NULL CHECK (
    reason IN ('plan_exhausted', 'hard_budget_reached', 'no_ready_tasks', 'shared_authority_failure')
  ),
  used_budget_units integer NOT NULL CHECK (used_budget_units >= 0),
  hard_budget_limit integer CHECK (hard_budget_limit IS NULL OR hard_budget_limit >= 0),
  policy_decision jsonb NOT NULL CHECK (
    jsonb_typeof(policy_decision) = 'object'
    AND policy_decision ?& ARRAY[
      'required_obligations', 'remaining_materiality',
      'next_information_gain', 'actionability', 'statistical_risk',
      'budget', 'next_task_id'
    ]
  ),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.capability_execution_snapshots (
  execution_snapshot_ref text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_context_ref text NOT NULL
    REFERENCES waje_runtime.authority_contexts(authority_context_ref) ON DELETE RESTRICT,
  plan_revision_id text NOT NULL UNIQUE
    REFERENCES waje_runtime.plan_revisions(plan_revision_id) ON DELETE RESTRICT,
  stop_ref text NOT NULL UNIQUE
    REFERENCES waje_runtime.exploration_stop_records(stop_ref) ON DELETE RESTRICT,
  outcome_set_digest text NOT NULL CHECK (length(outcome_set_digest) = 64),
  evidence_ledger_digest text NOT NULL CHECK (length(evidence_ledger_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE waje_runtime.capability_execution_snapshots
  DROP CONSTRAINT IF EXISTS capability_execution_snapshots_run_attempt_id_key;

CREATE INDEX IF NOT EXISTS idx_capability_execution_snapshots_run
  ON waje_runtime.capability_execution_snapshots(run_attempt_id, created_at);

-- Mutable worker coordination. These fields never grant analytical authority.
CREATE TABLE IF NOT EXISTS waje_runtime.capability_task_dispatches (
  task_id text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  plan_revision_id text NOT NULL
    REFERENCES waje_runtime.plan_revisions(plan_revision_id) ON DELETE RESTRICT,
  attempt_id text
    REFERENCES waje_runtime.capability_task_attempts(attempt_id) ON DELETE RESTRICT,
  dispatch_state text NOT NULL CHECK (dispatch_state IN ('pending', 'leased', 'terminal')),
  lease_owner text,
  lease_epoch bigint NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
  lease_expires_at timestamptz,
  accepted_outcome_ref text
    REFERENCES waje_runtime.capability_outcomes(outcome_ref) ON DELETE RESTRICT,
  updated_at timestamptz NOT NULL DEFAULT now()
);

DO $$
DECLARE authority_table text;
BEGIN
  FOREACH authority_table IN ARRAY ARRAY[
    'capability_task_attempts',
    'capability_failure_records',
    'capability_outcomes',
    'capability_evidence_ledger_entries',
    'exploration_stop_records',
    'capability_execution_snapshots'
  ]
  LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS %I_append_only ON waje_runtime.%I',
      authority_table,
      authority_table
    );
    EXECUTE format(
      'CREATE TRIGGER %I_append_only BEFORE UPDATE OR DELETE ON waje_runtime.%I '
      'FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation()',
      authority_table,
      authority_table
    );
  END LOOP;
END
$$;

CREATE INDEX IF NOT EXISTS idx_capability_task_attempts_plan
  ON waje_runtime.capability_task_attempts(plan_revision_id, task_id, execution_attempt);
CREATE INDEX IF NOT EXISTS idx_capability_outcomes_plan
  ON waje_runtime.capability_outcomes(plan_revision_id, task_id);
CREATE INDEX IF NOT EXISTS idx_capability_evidence_ledger_plan
  ON waje_runtime.capability_evidence_ledger_entries(plan_revision_id, task_id, evidence_ref);

-- vNext Phase 4-6 sealed authority, publication, and delivery. Every authority
-- record carries its owner and run scope directly. Composite foreign keys keep
-- child records inside that exact scope; content digests express equality but
-- never grant cross-owner access. The existing run_lifecycle_state_revisions
-- table remains the single append-only lifecycle authority.
CREATE TABLE IF NOT EXISTS waje_runtime.restricted_provider_responses (
  provider_response_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  attempt_id text NOT NULL,
  purpose text NOT NULL CHECK (
    purpose IN (
      'candidate_claim_proposal', 'claim_verification',
      'recommendation_proposal', 'recommendation_verification',
      'narrative_writer', 'block_verification'
    )
  ),
  provider_ref text NOT NULL,
  model_ref text NOT NULL,
  input_ref text NOT NULL,
  input_digest text NOT NULL CHECK (length(input_digest) = 64),
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  raw_response_content text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, provider_response_ref),
  UNIQUE(owner_ref, run_attempt_id, attempt_id),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, purpose, input_ref, input_digest, attempt_number)
);

CREATE TABLE IF NOT EXISTS waje_runtime.claim_authority_namespaces (
  authority_namespace_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  thread_ref text NOT NULL
    REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE RESTRICT,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(run_attempt_id),
  UNIQUE(owner_ref, run_attempt_id, authority_namespace_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest)
);

CREATE TABLE IF NOT EXISTS waje_runtime.claim_keys (
  claim_key text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_namespace_ref text NOT NULL,
  goal_id text NOT NULL,
  claim_kind text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, claim_key),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_namespace_ref)
    REFERENCES waje_runtime.claim_authority_namespaces(
      owner_ref, run_attempt_id, authority_namespace_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.claim_support_edges (
  support_edge_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_namespace_ref text NOT NULL,
  target_claim_key text NOT NULL,
  source_type text NOT NULL CHECK (source_type IN ('evidence', 'claim', 'assumption')),
  source_ref text NOT NULL,
  edge_kind text NOT NULL CHECK (
    edge_kind IN ('supports', 'qualifies', 'depends_on', 'contradicts', 'contextualizes')
  ),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, support_edge_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_namespace_ref)
    REFERENCES waje_runtime.claim_authority_namespaces(
      owner_ref, run_attempt_id, authority_namespace_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, target_claim_key)
    REFERENCES waje_runtime.claim_keys(owner_ref, run_attempt_id, claim_key)
    ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.claim_revisions (
  claim_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_namespace_ref text NOT NULL,
  claim_key text NOT NULL,
  claim_class text NOT NULL CHECK (
    claim_class IN (
      'observed_fact', 'accounting_identity_contribution',
      'dimension_localization', 'statistical_association',
      'candidate_mechanism', 'causal_effect', 'scenario', 'boundary'
    )
  ),
  claim_status text NOT NULL CHECK (claim_status IN ('proposed', 'verified', 'withheld')),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, claim_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_namespace_ref)
    REFERENCES waje_runtime.claim_authority_namespaces(
      owner_ref, run_attempt_id, authority_namespace_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, claim_key)
    REFERENCES waje_runtime.claim_keys(owner_ref, run_attempt_id, claim_key)
    ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.claim_settlement_checkpoints (
  checkpoint_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_namespace_ref text NOT NULL,
  execution_result_ref text NOT NULL,
  execution_result_digest text NOT NULL CHECK (length(execution_result_digest) = 64),
  plan_revision_id text NOT NULL
    REFERENCES waje_runtime.plan_revisions(plan_revision_id) ON DELETE RESTRICT,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, checkpoint_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, execution_result_ref, plan_revision_id),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_namespace_ref)
    REFERENCES waje_runtime.claim_authority_namespaces(
      owner_ref, run_attempt_id, authority_namespace_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.claim_obligation_settlement_bases (
  basis_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_namespace_ref text NOT NULL,
  checkpoint_ref text NOT NULL,
  obligation_id text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, basis_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, checkpoint_ref, obligation_id),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_namespace_ref)
    REFERENCES waje_runtime.claim_authority_namespaces(
      owner_ref, run_attempt_id, authority_namespace_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, checkpoint_ref)
    REFERENCES waje_runtime.claim_settlement_checkpoints(
      owner_ref, run_attempt_id, checkpoint_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.claim_verification_attempts (
  verification_attempt_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_namespace_ref text NOT NULL,
  checkpoint_ref text NOT NULL,
  authority_input_ref text NOT NULL,
  authority_input_digest text NOT NULL CHECK (length(authority_input_digest) = 64),
  provider_ref text NOT NULL,
  model_ref text NOT NULL,
  input_digest text NOT NULL CHECK (length(input_digest) = 64),
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  raw_provider_response_ref text NOT NULL,
  raw_provider_response_digest text NOT NULL CHECK (length(raw_provider_response_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, verification_attempt_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, authority_input_ref, input_digest, attempt_number),
  CHECK (authority_input_ref = checkpoint_ref),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_namespace_ref)
    REFERENCES waje_runtime.claim_authority_namespaces(
      owner_ref, run_attempt_id, authority_namespace_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, checkpoint_ref)
    REFERENCES waje_runtime.claim_settlement_checkpoints(
      owner_ref, run_attempt_id, checkpoint_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, raw_provider_response_ref)
    REFERENCES waje_runtime.restricted_provider_responses(
      owner_ref, run_attempt_id, provider_response_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.claim_verification_decisions (
  verification_decision_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  verification_attempt_ref text NOT NULL,
  subject_ref text NOT NULL,
  disposition text NOT NULL CHECK (disposition IN ('accepted', 'vetoed')),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, verification_decision_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, verification_attempt_ref, subject_ref),
  FOREIGN KEY (owner_ref, run_attempt_id, verification_attempt_ref)
    REFERENCES waje_runtime.claim_verification_attempts(
      owner_ref, run_attempt_id, verification_attempt_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.local_boundary_authorities (
  local_boundary_authority_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_namespace_ref text NOT NULL,
  checkpoint_ref text NOT NULL,
  checkpoint_digest text NOT NULL CHECK (length(checkpoint_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, local_boundary_authority_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, checkpoint_ref),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_namespace_ref)
    REFERENCES waje_runtime.claim_authority_namespaces(
      owner_ref, run_attempt_id, authority_namespace_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, checkpoint_ref)
    REFERENCES waje_runtime.claim_settlement_checkpoints(
      owner_ref, run_attempt_id, checkpoint_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.claim_verification_reports (
  verifier_report_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  verification_mode text NOT NULL CHECK (
    verification_mode IN ('semantic_verifier', 'local_boundary_authority')
  ),
  checkpoint_ref text NOT NULL,
  verification_attempt_ref text,
  local_boundary_authority_ref text,
  authority_input_ref text NOT NULL,
  authority_input_digest text NOT NULL CHECK (length(authority_input_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, verifier_report_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, verification_attempt_ref),
  CHECK (authority_input_ref = checkpoint_ref),
  CHECK (
    (
      verification_mode = 'semantic_verifier'
      AND verification_attempt_ref IS NOT NULL
      AND local_boundary_authority_ref IS NULL
    )
    OR (
      verification_mode = 'local_boundary_authority'
      AND verification_attempt_ref IS NULL
      AND local_boundary_authority_ref IS NOT NULL
    )
  ),
  FOREIGN KEY (owner_ref, run_attempt_id, verification_attempt_ref)
    REFERENCES waje_runtime.claim_verification_attempts(
      owner_ref, run_attempt_id, verification_attempt_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, local_boundary_authority_ref)
    REFERENCES waje_runtime.local_boundary_authorities(
      owner_ref, run_attempt_id, local_boundary_authority_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, checkpoint_ref)
    REFERENCES waje_runtime.claim_settlement_checkpoints(
      owner_ref, run_attempt_id, checkpoint_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.claim_obligation_coverages (
  coverage_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  obligation_id text NOT NULL,
  claim_verifier_report_ref text NOT NULL,
  coverage_state text NOT NULL CHECK (
    coverage_state IN (
      'satisfied', 'contradicted', 'mixed', 'unavailable',
      'unresolved', 'not_requested'
    )
  ),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, coverage_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, obligation_id),
  FOREIGN KEY (owner_ref, run_attempt_id, claim_verifier_report_ref)
    REFERENCES waje_runtime.claim_verification_reports(
      owner_ref, run_attempt_id, verifier_report_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.claim_graphs (
  claim_graph_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_namespace_ref text NOT NULL,
  authority_mode text NOT NULL CHECK (authority_mode IN ('claim_bearing', 'boundary_only')),
  claim_verifier_report_ref text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, claim_graph_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_namespace_ref)
    REFERENCES waje_runtime.claim_authority_namespaces(
      owner_ref, run_attempt_id, authority_namespace_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, claim_verifier_report_ref)
    REFERENCES waje_runtime.claim_verification_reports(
      owner_ref, run_attempt_id, verifier_report_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.claim_settlements (
  settlement_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  checkpoint_ref text NOT NULL,
  claim_graph_ref text NOT NULL,
  claim_graph_digest text NOT NULL CHECK (length(claim_graph_digest) = 64),
  execution_result_ref text NOT NULL,
  execution_result_digest text NOT NULL CHECK (length(execution_result_digest) = 64),
  claim_verifier_report_ref text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, settlement_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id),
  FOREIGN KEY (owner_ref, run_attempt_id, checkpoint_ref)
    REFERENCES waje_runtime.claim_settlement_checkpoints(
      owner_ref, run_attempt_id, checkpoint_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, claim_graph_ref)
    REFERENCES waje_runtime.claim_graphs(owner_ref, run_attempt_id, claim_graph_ref)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, claim_verifier_report_ref)
    REFERENCES waje_runtime.claim_verification_reports(
      owner_ref, run_attempt_id, verifier_report_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.recommendation_proposals (
  recommendation_proposal_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_namespace_ref text NOT NULL,
  claim_graph_ref text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, recommendation_proposal_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_namespace_ref)
    REFERENCES waje_runtime.claim_authority_namespaces(
      owner_ref, run_attempt_id, authority_namespace_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, claim_graph_ref)
    REFERENCES waje_runtime.claim_graphs(owner_ref, run_attempt_id, claim_graph_ref)
    ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.recommendation_verification_attempts (
  verification_attempt_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  recommendation_proposal_ref text NOT NULL,
  authority_input_ref text NOT NULL,
  authority_input_digest text NOT NULL CHECK (length(authority_input_digest) = 64),
  provider_ref text NOT NULL,
  model_ref text NOT NULL,
  input_digest text NOT NULL CHECK (length(input_digest) = 64),
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  raw_provider_response_ref text NOT NULL,
  raw_provider_response_digest text NOT NULL CHECK (length(raw_provider_response_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, verification_attempt_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, recommendation_proposal_ref, input_digest, attempt_number),
  FOREIGN KEY (owner_ref, run_attempt_id, recommendation_proposal_ref)
    REFERENCES waje_runtime.recommendation_proposals(
      owner_ref, run_attempt_id, recommendation_proposal_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, raw_provider_response_ref)
    REFERENCES waje_runtime.restricted_provider_responses(
      owner_ref, run_attempt_id, provider_response_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.recommendation_verification_decisions (
  verification_decision_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  verification_attempt_ref text NOT NULL,
  recommendation_proposal_ref text NOT NULL,
  disposition text NOT NULL CHECK (disposition IN ('accepted', 'vetoed')),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, verification_decision_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, verification_attempt_ref, recommendation_proposal_ref),
  FOREIGN KEY (owner_ref, run_attempt_id, verification_attempt_ref)
    REFERENCES waje_runtime.recommendation_verification_attempts(
      owner_ref, run_attempt_id, verification_attempt_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, recommendation_proposal_ref)
    REFERENCES waje_runtime.recommendation_proposals(
      owner_ref, run_attempt_id, recommendation_proposal_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.recommendation_records (
  recommendation_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  recommendation_proposal_ref text NOT NULL,
  verification_attempt_ref text NOT NULL,
  verification_decision_ref text NOT NULL,
  claim_graph_ref text NOT NULL,
  claim_verifier_report_ref text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, recommendation_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, recommendation_proposal_ref)
    REFERENCES waje_runtime.recommendation_proposals(
      owner_ref, run_attempt_id, recommendation_proposal_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, verification_attempt_ref)
    REFERENCES waje_runtime.recommendation_verification_attempts(
      owner_ref, run_attempt_id, verification_attempt_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, verification_decision_ref)
    REFERENCES waje_runtime.recommendation_verification_decisions(
      owner_ref, run_attempt_id, verification_decision_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, claim_graph_ref)
    REFERENCES waje_runtime.claim_graphs(owner_ref, run_attempt_id, claim_graph_ref)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, claim_verifier_report_ref)
    REFERENCES waje_runtime.claim_verification_reports(
      owner_ref, run_attempt_id, verifier_report_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.authority_bundles (
  bundle_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_namespace_ref text NOT NULL,
  bundle_revision integer NOT NULL CHECK (bundle_revision > 0),
  supersedes_bundle_ref text
    REFERENCES waje_runtime.authority_bundles(bundle_ref) ON DELETE RESTRICT,
  intent_revision_id text NOT NULL
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  plan_revision_id text NOT NULL
    REFERENCES waje_runtime.plan_revisions(plan_revision_id) ON DELETE RESTRICT,
  authority_context_ref text NOT NULL
    REFERENCES waje_runtime.authority_contexts(authority_context_ref) ON DELETE RESTRICT,
  execution_result_ref text NOT NULL,
  execution_result_digest text NOT NULL CHECK (length(execution_result_digest) = 64),
  claim_settlement_ref text NOT NULL,
  claim_settlement_digest text NOT NULL CHECK (length(claim_settlement_digest) = 64),
  claim_graph_ref text NOT NULL,
  claim_graph_digest text NOT NULL CHECK (length(claim_graph_digest) = 64),
  authority_mode text NOT NULL CHECK (authority_mode IN ('claim_bearing', 'boundary_only')),
  obligation_coverage_refs jsonb NOT NULL CHECK (jsonb_typeof(obligation_coverage_refs) = 'array'),
  claim_verifier_report_ref text NOT NULL,
  bundle_digest text NOT NULL CHECK (length(bundle_digest) = 64),
  seal_state text NOT NULL CHECK (seal_state = 'sealed'),
  sealed_at timestamptz NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, bundle_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, bundle_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_namespace_ref)
    REFERENCES waje_runtime.claim_authority_namespaces(
      owner_ref, run_attempt_id, authority_namespace_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, claim_settlement_ref)
    REFERENCES waje_runtime.claim_settlements(
      owner_ref, run_attempt_id, settlement_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, claim_graph_ref)
    REFERENCES waje_runtime.claim_graphs(owner_ref, run_attempt_id, claim_graph_ref)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, claim_verifier_report_ref)
    REFERENCES waje_runtime.claim_verification_reports(
      owner_ref, run_attempt_id, verifier_report_ref
    ) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_authority_bundles_one_sealed_per_run
  ON waje_runtime.authority_bundles(run_attempt_id)
  WHERE seal_state = 'sealed';

CREATE TABLE IF NOT EXISTS waje_runtime.publication_visibility_policies (
  policy_ref text NOT NULL,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  policy_id text NOT NULL,
  policy_revision integer NOT NULL CHECK (policy_revision > 0),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(owner_ref, run_attempt_id, policy_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, policy_id, policy_revision)
);

CREATE TABLE IF NOT EXISTS waje_runtime.public_claim_palettes (
  palette_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_bundle_ref text NOT NULL,
  authority_bundle_digest text NOT NULL CHECK (length(authority_bundle_digest) = 64),
  authority_mode text NOT NULL CHECK (authority_mode IN ('claim_bearing', 'boundary_only')),
  field_visibility_policy_ref text NOT NULL,
  field_visibility_policy_digest text NOT NULL CHECK (length(field_visibility_policy_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, palette_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, authority_bundle_ref, field_visibility_policy_ref),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_bundle_ref)
    REFERENCES waje_runtime.authority_bundles(owner_ref, run_attempt_id, bundle_ref)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, field_visibility_policy_ref)
    REFERENCES waje_runtime.publication_visibility_policies(
      owner_ref, run_attempt_id, policy_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.public_claims (
  public_claim_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  palette_ref text NOT NULL,
  claim_ref text NOT NULL,
  claim_key_ref text NOT NULL,
  claim_class text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, public_claim_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, palette_ref, claim_ref),
  FOREIGN KEY (owner_ref, run_attempt_id, palette_ref)
    REFERENCES waje_runtime.public_claim_palettes(
      owner_ref, run_attempt_id, palette_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, claim_ref)
    REFERENCES waje_runtime.claim_revisions(
      owner_ref, run_attempt_id, claim_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, claim_key_ref)
    REFERENCES waje_runtime.claim_keys(
      owner_ref, run_attempt_id, claim_key
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.public_fact_descriptors (
  fact_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  palette_ref text NOT NULL,
  public_claim_ref text NOT NULL,
  claim_ref text NOT NULL,
  source_material_ref text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, fact_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, palette_ref)
    REFERENCES waje_runtime.public_claim_palettes(
      owner_ref, run_attempt_id, palette_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, public_claim_ref)
    REFERENCES waje_runtime.public_claims(
      owner_ref, run_attempt_id, public_claim_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, claim_ref)
    REFERENCES waje_runtime.claim_revisions(
      owner_ref, run_attempt_id, claim_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.public_recommendations (
  public_recommendation_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  palette_ref text NOT NULL,
  recommendation_ref text NOT NULL,
  recommendation_digest text NOT NULL CHECK (length(recommendation_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, public_recommendation_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, palette_ref, recommendation_ref),
  FOREIGN KEY (owner_ref, run_attempt_id, palette_ref)
    REFERENCES waje_runtime.public_claim_palettes(
      owner_ref, run_attempt_id, palette_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, recommendation_ref)
    REFERENCES waje_runtime.recommendation_records(
      owner_ref, run_attempt_id, recommendation_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.public_limitations (
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  palette_ref text NOT NULL,
  limitation_ref text NOT NULL,
  limitation_handle text NOT NULL,
  public_context jsonb NOT NULL CHECK (jsonb_typeof(public_context) = 'object'),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(owner_ref, run_attempt_id, palette_ref, limitation_ref),
  UNIQUE(owner_ref, run_attempt_id, palette_ref, limitation_handle),
  UNIQUE(owner_ref, run_attempt_id, palette_ref, content_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, palette_ref)
    REFERENCES waje_runtime.public_claim_palettes(
      owner_ref, run_attempt_id, palette_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.narrative_material_projections (
  projection_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  palette_ref text NOT NULL,
  palette_digest text NOT NULL CHECK (length(palette_digest) = 64),
  claim_settlement_ref text NOT NULL,
  claim_settlement_digest text NOT NULL CHECK (length(claim_settlement_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, projection_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, palette_ref, claim_settlement_ref),
  FOREIGN KEY (owner_ref, run_attempt_id, palette_ref)
    REFERENCES waje_runtime.public_claim_palettes(
      owner_ref, run_attempt_id, palette_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, claim_settlement_ref)
    REFERENCES waje_runtime.claim_settlements(
      owner_ref, run_attempt_id, settlement_ref
    ) ON DELETE RESTRICT
);

-- Controlled investigation is an advisory, read-only child lifecycle between
-- the sealed authority bundle and the one customer narrative. Operations are
-- immutable authority bindings. Dispatch rows contain an immutable child
-- command envelope plus mutable lease and terminal coordination.
CREATE TABLE IF NOT EXISTS waje_runtime.controlled_investigation_operations (
  operation_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  thread_ref text NOT NULL
    REFERENCES waje_runtime.investigation_threads(thread_id) ON DELETE RESTRICT,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  intent_revision_id text NOT NULL
    REFERENCES waje_runtime.intent_revisions(intent_revision_id) ON DELETE RESTRICT,
  plan_revision_id text NOT NULL
    REFERENCES waje_runtime.plan_revisions(plan_revision_id) ON DELETE RESTRICT,
  authority_context_ref text NOT NULL
    REFERENCES waje_runtime.authority_contexts(authority_context_ref) ON DELETE RESTRICT,
  authority_bundle_ref text NOT NULL,
  parent_transition_id text NOT NULL
    REFERENCES waje_runtime.workflow_transition_attempts(attempt_id)
      ON DELETE RESTRICT,
  source_material_projection_ref text NOT NULL,
  source_material_projection_digest text NOT NULL
    CHECK (length(source_material_projection_digest) = 64),
  input_digest text NOT NULL CHECK (length(input_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(operation_ref, thread_ref),
  UNIQUE(run_attempt_id, input_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_bundle_ref)
    REFERENCES waje_runtime.authority_bundles(
      owner_ref, run_attempt_id, bundle_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, source_material_projection_ref)
    REFERENCES waje_runtime.narrative_material_projections(
      owner_ref, run_attempt_id, projection_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.controlled_investigation_dispatches (
  investigation_ref text PRIMARY KEY,
  operation_ref text NOT NULL,
  thread_ref text NOT NULL,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  child_run_id text NOT NULL UNIQUE,
  investigation_key text NOT NULL,
  question text NOT NULL,
  axis_refs jsonb NOT NULL CHECK (jsonb_typeof(axis_refs) = 'array'),
  allowed_source_refs jsonb NOT NULL
    CHECK (jsonb_typeof(allowed_source_refs) = 'array'),
  allowed_source_set_digest text NOT NULL
    CHECK (length(allowed_source_set_digest) = 64),
  expected_output_kind text NOT NULL CHECK (
    expected_output_kind IN (
      'mechanism_explanation',
      'structure_concentration',
      'alternative_explanation'
    )
  ),
  input_digest text NOT NULL CHECK (length(input_digest) = 64),
  idempotency_key text NOT NULL CHECK (length(idempotency_key) = 64),
  command_payload jsonb NOT NULL CHECK (jsonb_typeof(command_payload) = 'object'),
  dispatch_state text NOT NULL DEFAULT 'planned' CHECK (
    dispatch_state IN ('planned', 'leased', 'running', 'terminal')
  ),
  terminal_status text CHECK (
    terminal_status IN ('completed', 'limited', 'failed', 'cancelled')
  ),
  lease_owner_id text,
  lease_epoch bigint NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
  lease_expires_at timestamptz,
  heartbeat_at timestamptz,
  accepted_attempt_ref text,
  accepted_artifact_ref text,
  output_digest text CHECK (
    output_digest IS NULL OR length(output_digest) = 64
  ),
  failure_code text,
  retryability text CHECK (
    retryability IN ('retryable', 'not_retryable')
  ),
  technical_detail_ref text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(operation_ref, investigation_key),
  UNIQUE(run_attempt_id, input_digest),
  UNIQUE(run_attempt_id, idempotency_key),
  FOREIGN KEY (operation_ref, thread_ref)
    REFERENCES waje_runtime.controlled_investigation_operations(
      operation_ref, thread_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (run_attempt_id, accepted_attempt_ref)
    REFERENCES waje_runtime.durable_call_acceptances(
      run_attempt_id, accepted_attempt_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (thread_ref, accepted_artifact_ref)
    REFERENCES waje_runtime.agent_generated_artifacts(
      thread_id, artifact_ref
    ) ON DELETE RESTRICT,
  CHECK (
    (dispatch_state = 'planned'
      AND lease_owner_id IS NULL AND lease_expires_at IS NULL
      AND terminal_status IS NULL)
    OR (dispatch_state IN ('leased', 'running')
      AND lease_owner_id IS NOT NULL AND lease_expires_at IS NOT NULL
      AND terminal_status IS NULL)
    OR (dispatch_state = 'terminal'
      AND lease_owner_id IS NULL AND lease_expires_at IS NULL
      AND terminal_status IS NOT NULL)
  ),
  CHECK (
    (terminal_status IN ('completed', 'limited')
      AND accepted_attempt_ref IS NOT NULL
      AND accepted_artifact_ref IS NOT NULL
      AND output_digest IS NOT NULL
      AND failure_code IS NULL)
    OR (terminal_status = 'failed'
      AND accepted_attempt_ref IS NULL
      AND accepted_artifact_ref IS NULL
      AND output_digest IS NULL
      AND failure_code IS NOT NULL
      AND retryability IS NOT NULL)
    OR (terminal_status = 'cancelled'
      AND accepted_attempt_ref IS NULL
      AND accepted_artifact_ref IS NULL
      AND output_digest IS NULL)
    OR terminal_status IS NULL
  )
);

CREATE INDEX IF NOT EXISTS idx_controlled_investigation_dispatch_recovery
  ON waje_runtime.controlled_investigation_dispatches(
    dispatch_state, lease_expires_at, updated_at, investigation_ref
  )
  WHERE dispatch_state IN ('planned', 'leased', 'running');

CREATE OR REPLACE FUNCTION
waje_runtime.controlled_investigation_dispatch_identity_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.investigation_ref IS DISTINCT FROM OLD.investigation_ref
     OR NEW.operation_ref IS DISTINCT FROM OLD.operation_ref
     OR NEW.thread_ref IS DISTINCT FROM OLD.thread_ref
     OR NEW.run_attempt_id IS DISTINCT FROM OLD.run_attempt_id
     OR NEW.child_run_id IS DISTINCT FROM OLD.child_run_id
     OR NEW.investigation_key IS DISTINCT FROM OLD.investigation_key
     OR NEW.question IS DISTINCT FROM OLD.question
     OR NEW.axis_refs IS DISTINCT FROM OLD.axis_refs
     OR NEW.allowed_source_refs IS DISTINCT FROM OLD.allowed_source_refs
     OR NEW.allowed_source_set_digest IS DISTINCT FROM OLD.allowed_source_set_digest
     OR NEW.expected_output_kind IS DISTINCT FROM OLD.expected_output_kind
     OR NEW.input_digest IS DISTINCT FROM OLD.input_digest
     OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
     OR NEW.command_payload IS DISTINCT FROM OLD.command_payload
     OR NEW.created_at IS DISTINCT FROM OLD.created_at
  THEN
    RAISE EXCEPTION 'controlled_investigation_dispatch_identity_immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS controlled_investigation_dispatch_identity_immutable
  ON waje_runtime.controlled_investigation_dispatches;
CREATE TRIGGER controlled_investigation_dispatch_identity_immutable
  BEFORE UPDATE ON waje_runtime.controlled_investigation_dispatches
  FOR EACH ROW
  EXECUTE FUNCTION
    waje_runtime.controlled_investigation_dispatch_identity_immutable();

CREATE TABLE IF NOT EXISTS waje_runtime.narrative_writer_attempts (
  writer_attempt_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  attempt_id text NOT NULL,
  authority_bundle_ref text NOT NULL,
  material_projection_ref text NOT NULL,
  input_ref text NOT NULL,
  input_digest text NOT NULL CHECK (length(input_digest) = 64),
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  provider_ref text NOT NULL,
  model_ref text NOT NULL,
  provider_response_ref text NOT NULL,
  provider_response_digest text NOT NULL CHECK (length(provider_response_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, writer_attempt_ref),
  UNIQUE(owner_ref, run_attempt_id, attempt_id),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, input_ref, input_digest, attempt_number),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_bundle_ref)
    REFERENCES waje_runtime.authority_bundles(
      owner_ref, run_attempt_id, bundle_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, material_projection_ref)
    REFERENCES waje_runtime.narrative_material_projections(
      owner_ref, run_attempt_id, projection_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, provider_response_ref)
    REFERENCES waje_runtime.restricted_provider_responses(
      owner_ref, run_attempt_id, provider_response_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.narrative_documents (
  narrative_id text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_bundle_ref text NOT NULL,
  material_projection_ref text NOT NULL,
  writer_attempt_ref text NOT NULL,
  parent_narrative_id text,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, narrative_id),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, writer_attempt_ref),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_bundle_ref)
    REFERENCES waje_runtime.authority_bundles(owner_ref, run_attempt_id, bundle_ref)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, material_projection_ref)
    REFERENCES waje_runtime.narrative_material_projections(owner_ref, run_attempt_id, projection_ref)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, writer_attempt_ref)
    REFERENCES waje_runtime.narrative_writer_attempts(
      owner_ref, run_attempt_id, writer_attempt_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, parent_narrative_id)
    REFERENCES waje_runtime.narrative_documents(owner_ref, run_attempt_id, narrative_id)
    ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.narrative_blocks (
  block_id text NOT NULL,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  narrative_id text NOT NULL,
  writer_attempt_id text NOT NULL,
  role text NOT NULL CHECK (
    role IN (
      'executive_answer', 'direction', 'accounting_drivers',
      'dimension_localization', 'contextual_pattern', 'boundary', 'next_action'
    )
  ),
  required boolean NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(owner_ref, run_attempt_id, narrative_id, block_id),
  UNIQUE(owner_ref, run_attempt_id, narrative_id, content_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_id)
    REFERENCES waje_runtime.narrative_documents(
      owner_ref, run_attempt_id, narrative_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, writer_attempt_id)
    REFERENCES waje_runtime.narrative_writer_attempts(
      owner_ref, run_attempt_id, attempt_id
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.narrative_fact_bindings (
  binding_ref text NOT NULL,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  narrative_id text NOT NULL,
  block_id text NOT NULL,
  claim_handle text NOT NULL,
  fact_handle text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(owner_ref, run_attempt_id, narrative_id, block_id, binding_ref),
  UNIQUE(owner_ref, run_attempt_id, narrative_id, block_id, content_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_id)
    REFERENCES waje_runtime.narrative_documents(
      owner_ref, run_attempt_id, narrative_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_id, block_id)
    REFERENCES waje_runtime.narrative_blocks(
      owner_ref, run_attempt_id, narrative_id, block_id
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.sensitive_output_findings (
  finding_ref text NOT NULL,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  narrative_id text NOT NULL,
  block_id text NOT NULL,
  field_visibility_policy_ref text NOT NULL,
  policy_rule_ref text NOT NULL,
  material_ref text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(owner_ref, run_attempt_id, narrative_id, finding_ref),
  UNIQUE(owner_ref, run_attempt_id, narrative_id, content_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_id)
    REFERENCES waje_runtime.narrative_documents(owner_ref, run_attempt_id, narrative_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_id, block_id)
    REFERENCES waje_runtime.narrative_blocks(owner_ref, run_attempt_id, narrative_id, block_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, field_visibility_policy_ref)
    REFERENCES waje_runtime.publication_visibility_policies(
      owner_ref, run_attempt_id, policy_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.block_local_validation_reports (
  local_report_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  narrative_id text NOT NULL,
  narrative_digest text NOT NULL CHECK (length(narrative_digest) = 64),
  material_projection_ref text NOT NULL,
  material_projection_digest text NOT NULL CHECK (length(material_projection_digest) = 64),
  field_visibility_policy_ref text NOT NULL,
  finding_refs jsonb NOT NULL CHECK (jsonb_typeof(finding_refs) = 'array'),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, local_report_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, narrative_id, content_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_id)
    REFERENCES waje_runtime.narrative_documents(owner_ref, run_attempt_id, narrative_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, material_projection_ref)
    REFERENCES waje_runtime.narrative_material_projections(owner_ref, run_attempt_id, projection_ref)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, field_visibility_policy_ref)
    REFERENCES waje_runtime.publication_visibility_policies(
      owner_ref, run_attempt_id, policy_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.block_local_issues (
  issue_ref text NOT NULL,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  local_report_ref text NOT NULL,
  narrative_id text NOT NULL,
  block_id text NOT NULL,
  issue_code text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(owner_ref, run_attempt_id, local_report_ref, issue_ref),
  UNIQUE(owner_ref, run_attempt_id, local_report_ref, content_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, local_report_ref)
    REFERENCES waje_runtime.block_local_validation_reports(
      owner_ref, run_attempt_id, local_report_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_id, block_id)
    REFERENCES waje_runtime.narrative_blocks(
      owner_ref, run_attempt_id, narrative_id, block_id
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.block_verification_attempts (
  verification_attempt_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  narrative_id text NOT NULL,
  narrative_digest text NOT NULL CHECK (length(narrative_digest) = 64),
  local_report_ref text NOT NULL,
  local_report_digest text NOT NULL CHECK (length(local_report_digest) = 64),
  attempt_id text NOT NULL,
  input_ref text NOT NULL,
  input_digest text NOT NULL CHECK (length(input_digest) = 64),
  provider_ref text NOT NULL,
  model_ref text NOT NULL,
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  provider_response_ref text NOT NULL,
  provider_response_digest text NOT NULL CHECK (length(provider_response_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, verification_attempt_ref),
  UNIQUE(owner_ref, run_attempt_id, attempt_id),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, narrative_id, input_ref, input_digest, attempt_number),
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_id)
    REFERENCES waje_runtime.narrative_documents(owner_ref, run_attempt_id, narrative_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, local_report_ref)
    REFERENCES waje_runtime.block_local_validation_reports(
      owner_ref, run_attempt_id, local_report_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, provider_response_ref)
    REFERENCES waje_runtime.restricted_provider_responses(
      owner_ref, run_attempt_id, provider_response_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.block_vetoes (
  veto_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  verification_attempt_ref text NOT NULL,
  narrative_id text NOT NULL,
  block_id text NOT NULL,
  reason_code text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, veto_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, verification_attempt_ref, block_id),
  FOREIGN KEY (owner_ref, run_attempt_id, verification_attempt_ref)
    REFERENCES waje_runtime.block_verification_attempts(
      owner_ref, run_attempt_id, verification_attempt_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_id)
    REFERENCES waje_runtime.narrative_documents(
      owner_ref, run_attempt_id, narrative_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_id, block_id)
    REFERENCES waje_runtime.narrative_blocks(
      owner_ref, run_attempt_id, narrative_id, block_id
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.block_verification_reports (
  verifier_report_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  audit_status text NOT NULL CHECK (
    audit_status IN ('pending', 'completed', 'unavailable')
  ),
  verification_attempt_ref text,
  verification_attempt_digest text CHECK (
    verification_attempt_digest IS NULL OR length(verification_attempt_digest) = 64
  ),
  narrative_id text NOT NULL,
  narrative_digest text NOT NULL CHECK (length(narrative_digest) = 64),
  local_report_ref text NOT NULL,
  local_report_digest text NOT NULL CHECK (length(local_report_digest) = 64),
  failure_kind text,
  retryability text CHECK (
    retryability IS NULL OR retryability IN ('retryable', 'not_retryable')
  ),
  technical_detail_ref text,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (
    (
      audit_status = 'pending'
      AND verification_attempt_ref IS NULL
      AND verification_attempt_digest IS NULL
      AND failure_kind IS NULL
      AND retryability IS NULL
      AND technical_detail_ref IS NULL
    )
    OR
    (
      audit_status = 'completed'
      AND verification_attempt_ref IS NOT NULL
      AND verification_attempt_digest IS NOT NULL
      AND failure_kind IS NULL
      AND retryability IS NULL
      AND technical_detail_ref IS NULL
    )
    OR
    (
      audit_status = 'unavailable'
      AND verification_attempt_ref IS NULL
      AND verification_attempt_digest IS NULL
      AND failure_kind IS NOT NULL
      AND retryability IS NOT NULL
      AND technical_detail_ref IS NOT NULL
    )
  ),
  UNIQUE(owner_ref, run_attempt_id, verifier_report_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, verification_attempt_ref),
  FOREIGN KEY (owner_ref, run_attempt_id, verification_attempt_ref)
    REFERENCES waje_runtime.block_verification_attempts(
      owner_ref, run_attempt_id, verification_attempt_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_id)
    REFERENCES waje_runtime.narrative_documents(owner_ref, run_attempt_id, narrative_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, local_report_ref)
    REFERENCES waje_runtime.block_local_validation_reports(
      owner_ref, run_attempt_id, local_report_ref
    ) ON DELETE RESTRICT
);

ALTER TABLE waje_runtime.block_verification_reports
  ADD COLUMN IF NOT EXISTS audit_status text;
ALTER TABLE waje_runtime.block_verification_reports
  ADD COLUMN IF NOT EXISTS failure_kind text;
ALTER TABLE waje_runtime.block_verification_reports
  ADD COLUMN IF NOT EXISTS retryability text;
ALTER TABLE waje_runtime.block_verification_reports
  ADD COLUMN IF NOT EXISTS technical_detail_ref text;
ALTER TABLE waje_runtime.block_verification_reports
  ALTER COLUMN verification_attempt_ref DROP NOT NULL;
ALTER TABLE waje_runtime.block_verification_reports
  ALTER COLUMN verification_attempt_digest DROP NOT NULL;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgrelid = 'waje_runtime.block_verification_reports'::regclass
      AND tgname = 'block_verification_reports_append_only'
      AND NOT tgisinternal
  ) THEN
    ALTER TABLE waje_runtime.block_verification_reports
      DISABLE TRIGGER block_verification_reports_append_only;
  END IF;
END
$$;
UPDATE waje_runtime.block_verification_reports
SET audit_status = 'completed'
WHERE audit_status IS NULL;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgrelid = 'waje_runtime.block_verification_reports'::regclass
      AND tgname = 'block_verification_reports_append_only'
      AND NOT tgisinternal
  ) THEN
    ALTER TABLE waje_runtime.block_verification_reports
      ENABLE TRIGGER block_verification_reports_append_only;
  END IF;
END
$$;
ALTER TABLE waje_runtime.block_verification_reports
  ALTER COLUMN audit_status SET NOT NULL;
DO $$
DECLARE existing_check record;
BEGIN
  FOR existing_check IN
    SELECT constraint_record.conname AS constraint_name
    FROM pg_constraint constraint_record
    WHERE constraint_record.conrelid =
      'waje_runtime.block_verification_reports'::regclass
      AND constraint_record.contype = 'c'
      AND pg_get_constraintdef(constraint_record.oid) LIKE '%audit_status%'
  LOOP
    EXECUTE format(
      'ALTER TABLE waje_runtime.block_verification_reports DROP CONSTRAINT %I',
      existing_check.constraint_name
    );
  END LOOP;
END
$$;
ALTER TABLE waje_runtime.block_verification_reports
  ADD CONSTRAINT block_verification_reports_audit_status_check
  CHECK (audit_status IN ('pending', 'completed', 'unavailable'));
ALTER TABLE waje_runtime.block_verification_reports
  ADD CONSTRAINT block_verification_reports_audit_shape_check
  CHECK (
    (
      audit_status = 'pending'
      AND verification_attempt_ref IS NULL
      AND verification_attempt_digest IS NULL
      AND failure_kind IS NULL
      AND retryability IS NULL
      AND technical_detail_ref IS NULL
    )
    OR
    (
      audit_status = 'completed'
      AND verification_attempt_ref IS NOT NULL
      AND verification_attempt_digest IS NOT NULL
      AND failure_kind IS NULL
      AND retryability IS NULL
      AND technical_detail_ref IS NULL
    )
    OR
    (
      audit_status = 'unavailable'
      AND verification_attempt_ref IS NULL
      AND verification_attempt_digest IS NULL
      AND failure_kind IS NOT NULL
      AND retryability IN ('retryable', 'not_retryable')
      AND technical_detail_ref IS NOT NULL
    )
  );

CREATE TABLE IF NOT EXISTS waje_runtime.publication_projections (
  projection_id text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  authority_bundle_ref text NOT NULL,
  authority_bundle_digest text NOT NULL CHECK (length(authority_bundle_digest) = 64),
  material_projection_ref text NOT NULL,
  material_projection_digest text NOT NULL CHECK (length(material_projection_digest) = 64),
  narrative_id text NOT NULL,
  narrative_digest text NOT NULL CHECK (length(narrative_digest) = 64),
  local_report_ref text NOT NULL,
  local_report_digest text NOT NULL CHECK (length(local_report_digest) = 64),
  block_verifier_report_ref text,
  block_verifier_report_digest text CHECK (
    block_verifier_report_digest IS NULL
    OR length(block_verifier_report_digest) = 64
  ),
  field_visibility_policy_ref text NOT NULL,
  field_visibility_policy_digest text NOT NULL CHECK (length(field_visibility_policy_digest) = 64),
  recommendation_refs jsonb NOT NULL CHECK (jsonb_typeof(recommendation_refs) = 'array'),
  projection_digest text NOT NULL CHECK (length(projection_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, projection_id),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, projection_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, authority_bundle_ref)
    REFERENCES waje_runtime.authority_bundles(owner_ref, run_attempt_id, bundle_ref)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, material_projection_ref)
    REFERENCES waje_runtime.narrative_material_projections(owner_ref, run_attempt_id, projection_ref)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_id)
    REFERENCES waje_runtime.narrative_documents(owner_ref, run_attempt_id, narrative_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, local_report_ref)
    REFERENCES waje_runtime.block_local_validation_reports(
      owner_ref, run_attempt_id, local_report_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, block_verifier_report_ref)
    REFERENCES waje_runtime.block_verification_reports(
      owner_ref, run_attempt_id, verifier_report_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, field_visibility_policy_ref)
    REFERENCES waje_runtime.publication_visibility_policies(
      owner_ref, run_attempt_id, policy_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.publication_revisions (
  publication_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  revision integer NOT NULL CHECK (revision > 0),
  supersedes_publication_ref text,
  authority_bundle_ref text NOT NULL,
  authority_bundle_digest text NOT NULL CHECK (length(authority_bundle_digest) = 64),
  narrative_id text NOT NULL,
  narrative_digest text NOT NULL CHECK (length(narrative_digest) = 64),
  narrative_attempt_id text NOT NULL,
  local_report_ref text NOT NULL,
  local_report_digest text NOT NULL CHECK (length(local_report_digest) = 64),
  block_verifier_report_ref text,
  block_verifier_report_digest text CHECK (
    block_verifier_report_digest IS NULL
    OR length(block_verifier_report_digest) = 64
  ),
  projection_id text NOT NULL,
  projection_digest text NOT NULL CHECK (length(projection_digest) = 64),
  publication_digest text NOT NULL CHECK (length(publication_digest) = 64),
  published_at timestamptz NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, publication_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, publication_digest),
  UNIQUE(owner_ref, run_attempt_id, revision),
  UNIQUE(owner_ref, run_attempt_id, supersedes_publication_ref),
  CHECK (
    (revision = 1 AND supersedes_publication_ref IS NULL)
    OR (revision > 1 AND supersedes_publication_ref IS NOT NULL)
  ),
  FOREIGN KEY (owner_ref, run_attempt_id, supersedes_publication_ref)
    REFERENCES waje_runtime.publication_revisions(
      owner_ref, run_attempt_id, publication_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, authority_bundle_ref)
    REFERENCES waje_runtime.authority_bundles(owner_ref, run_attempt_id, bundle_ref)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_id)
    REFERENCES waje_runtime.narrative_documents(owner_ref, run_attempt_id, narrative_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_attempt_id)
    REFERENCES waje_runtime.narrative_writer_attempts(owner_ref, run_attempt_id, attempt_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, local_report_ref)
    REFERENCES waje_runtime.block_local_validation_reports(
      owner_ref, run_attempt_id, local_report_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, block_verifier_report_ref)
    REFERENCES waje_runtime.block_verification_reports(
      owner_ref, run_attempt_id, verifier_report_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, projection_id)
    REFERENCES waje_runtime.publication_projections(
      owner_ref, run_attempt_id, projection_id
    ) ON DELETE RESTRICT
);

ALTER TABLE waje_runtime.publication_projections
  ALTER COLUMN block_verifier_report_ref DROP NOT NULL,
  ALTER COLUMN block_verifier_report_digest DROP NOT NULL;

ALTER TABLE waje_runtime.publication_revisions
  ALTER COLUMN block_verifier_report_ref DROP NOT NULL,
  ALTER COLUMN block_verifier_report_digest DROP NOT NULL;

CREATE TABLE IF NOT EXISTS waje_runtime.delivery_outbox_records (
  outbox_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  publication_ref text NOT NULL,
  publication_digest text NOT NULL CHECK (length(publication_digest) = 64),
  authority_bundle_ref text NOT NULL,
  authority_bundle_digest text NOT NULL CHECK (length(authority_bundle_digest) = 64),
  projection_id text NOT NULL,
  projection_digest text NOT NULL CHECK (length(projection_digest) = 64),
  destination_ref text NOT NULL,
  channel text NOT NULL,
  idempotency_key text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, outbox_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, publication_ref, destination_ref, channel),
  UNIQUE(owner_ref, run_attempt_id, idempotency_key),
  FOREIGN KEY (owner_ref, run_attempt_id, publication_ref)
    REFERENCES waje_runtime.publication_revisions(
      owner_ref, run_attempt_id, publication_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, authority_bundle_ref)
    REFERENCES waje_runtime.authority_bundles(owner_ref, run_attempt_id, bundle_ref)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, projection_id)
    REFERENCES waje_runtime.publication_projections(
      owner_ref, run_attempt_id, projection_id
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.publication_customer_payloads (
  customer_payload_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  outbox_ref text NOT NULL,
  publication_ref text NOT NULL,
  publication_digest text NOT NULL CHECK (length(publication_digest) = 64),
  projection_id text NOT NULL,
  projection_digest text NOT NULL CHECK (length(projection_digest) = 64),
  field_visibility_policy_ref text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  customer_payload jsonb NOT NULL CHECK (jsonb_typeof(customer_payload) = 'object'),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  customer_payload_digest text GENERATED ALWAYS AS (
    payload->>'customer_payload_digest'
  ) STORED,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, customer_payload_ref),
  UNIQUE(owner_ref, run_attempt_id, outbox_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  FOREIGN KEY (owner_ref, run_attempt_id, outbox_ref)
    REFERENCES waje_runtime.delivery_outbox_records(
      owner_ref, run_attempt_id, outbox_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, publication_ref)
    REFERENCES waje_runtime.publication_revisions(
      owner_ref, run_attempt_id, publication_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, projection_id)
    REFERENCES waje_runtime.publication_projections(
      owner_ref, run_attempt_id, projection_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, field_visibility_policy_ref)
    REFERENCES waje_runtime.publication_visibility_policies(
      owner_ref, run_attempt_id, policy_ref
    ) ON DELETE RESTRICT
);

ALTER TABLE waje_runtime.publication_customer_payloads
  ADD COLUMN IF NOT EXISTS customer_payload_digest text GENERATED ALWAYS AS (
    payload->>'customer_payload_digest'
  ) STORED;

ALTER TABLE waje_runtime.publication_customer_payloads
  ALTER COLUMN customer_payload_digest SET NOT NULL,
  DROP CONSTRAINT IF EXISTS publication_customer_payloads_customer_payload_digest_check;

ALTER TABLE waje_runtime.publication_customer_payloads
  ADD CONSTRAINT publication_customer_payloads_customer_payload_digest_check
  CHECK (length(customer_payload_digest) = 64) NOT VALID;

ALTER TABLE waje_runtime.publication_customer_payloads
  VALIDATE CONSTRAINT publication_customer_payloads_customer_payload_digest_check;

CREATE TABLE IF NOT EXISTS waje_runtime.delivery_attempts (
  attempt_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  outbox_ref text NOT NULL,
  publication_ref text NOT NULL,
  publication_digest text NOT NULL CHECK (length(publication_digest) = 64),
  projection_id text NOT NULL,
  projection_digest text NOT NULL CHECK (length(projection_digest) = 64),
  destination_ref text NOT NULL,
  channel text NOT NULL,
  idempotency_key text NOT NULL,
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  previous_attempt_ref text,
  status text NOT NULL CHECK (
    status IN ('published', 'retryable_failed', 'permanently_failed')
  ),
  transport_receipt_ref text,
  failure_code text,
  attempted_at timestamptz NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, attempt_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, outbox_ref, attempt_number),
  CHECK (
    (status = 'published' AND transport_receipt_ref IS NOT NULL AND failure_code IS NULL)
    OR (status <> 'published' AND transport_receipt_ref IS NULL AND failure_code IS NOT NULL)
  ),
  FOREIGN KEY (owner_ref, run_attempt_id, outbox_ref)
    REFERENCES waje_runtime.delivery_outbox_records(
      owner_ref, run_attempt_id, outbox_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, publication_ref)
    REFERENCES waje_runtime.publication_revisions(
      owner_ref, run_attempt_id, publication_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, projection_id)
    REFERENCES waje_runtime.publication_projections(
      owner_ref, run_attempt_id, projection_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, previous_attempt_ref)
    REFERENCES waje_runtime.delivery_attempts(owner_ref, run_attempt_id, attempt_ref)
    ON DELETE RESTRICT
);

-- Mutable operational coordination. It can schedule and lease an immutable
-- outbox command, while carrying no analytical or publication authority.
CREATE TABLE IF NOT EXISTS waje_runtime.delivery_dispatches (
  outbox_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  dispatch_state text NOT NULL CHECK (
    dispatch_state IN ('pending', 'leased', 'retry_scheduled', 'terminal')
  ),
  lease_owner text,
  lease_epoch bigint NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
  lease_expires_at timestamptz,
  next_attempt_at timestamptz,
  accepted_attempt_ref text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (owner_ref, run_attempt_id, outbox_ref)
    REFERENCES waje_runtime.delivery_outbox_records(
      owner_ref, run_attempt_id, outbox_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, accepted_attempt_ref)
    REFERENCES waje_runtime.delivery_attempts(owner_ref, run_attempt_id, attempt_ref)
    ON DELETE RESTRICT,
  CHECK (
    dispatch_state <> 'leased'
    OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS waje_runtime.customer_publications (
  customer_publication_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  outbox_ref text NOT NULL,
  delivery_attempt_ref text NOT NULL,
  publication_ref text NOT NULL,
  projection_id text NOT NULL,
  destination_ref text NOT NULL,
  channel text NOT NULL,
  transport_receipt_ref text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, customer_publication_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, outbox_ref),
  FOREIGN KEY (owner_ref, run_attempt_id, outbox_ref)
    REFERENCES waje_runtime.delivery_outbox_records(
      owner_ref, run_attempt_id, outbox_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, delivery_attempt_ref)
    REFERENCES waje_runtime.delivery_attempts(owner_ref, run_attempt_id, attempt_ref)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, publication_ref)
    REFERENCES waje_runtime.publication_revisions(
      owner_ref, run_attempt_id, publication_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, projection_id)
    REFERENCES waje_runtime.publication_projections(
      owner_ref, run_attempt_id, projection_id
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.narrative_quality_audit_results (
  audit_result_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  source_customer_publication_ref text NOT NULL,
  narrative_workflow_ref text NOT NULL,
  narrative_workflow_digest text NOT NULL CHECK (length(narrative_workflow_digest) = 64),
  call_input_ref text NOT NULL,
  call_input_digest text NOT NULL CHECK (length(call_input_digest) = 64),
  verifier_report_ref text NOT NULL,
  verifier_report_digest text NOT NULL CHECK (length(verifier_report_digest) = 64),
  audit_status text NOT NULL CHECK (
    audit_status IN ('completed', 'unavailable')
  ),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, audit_result_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, source_customer_publication_ref, call_input_ref),
  FOREIGN KEY (owner_ref, run_attempt_id, source_customer_publication_ref)
    REFERENCES waje_runtime.customer_publications(
      owner_ref, run_attempt_id, customer_publication_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, verifier_report_ref)
    REFERENCES waje_runtime.block_verification_reports(
      owner_ref, run_attempt_id, verifier_report_ref
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.narrative_attempt_requests (
  request_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  source_publication_ref text NOT NULL,
  source_publication_digest text NOT NULL CHECK (length(source_publication_digest) = 64),
  authority_bundle_ref text NOT NULL,
  authority_bundle_digest text NOT NULL CHECK (length(authority_bundle_digest) = 64),
  source_narrative_id text NOT NULL,
  source_narrative_attempt_id text NOT NULL,
  requested_attempt_id text NOT NULL,
  reason_dimensions jsonb NOT NULL CHECK (jsonb_typeof(reason_dimensions) = 'array'),
  requested_by text NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, request_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(owner_ref, run_attempt_id, requested_attempt_id),
  FOREIGN KEY (owner_ref, run_attempt_id, source_publication_ref)
    REFERENCES waje_runtime.publication_revisions(
      owner_ref, run_attempt_id, publication_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, authority_bundle_ref)
    REFERENCES waje_runtime.authority_bundles(owner_ref, run_attempt_id, bundle_ref)
    ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, source_narrative_id)
    REFERENCES waje_runtime.narrative_documents(owner_ref, run_attempt_id, narrative_id)
    ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS waje_runtime.insight_quality_evaluations (
  evaluation_ref text PRIMARY KEY,
  owner_ref text NOT NULL,
  run_attempt_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  source_publication_ref text NOT NULL,
  source_publication_digest text NOT NULL CHECK (length(source_publication_digest) = 64),
  authority_bundle_ref text NOT NULL,
  authority_bundle_digest text NOT NULL CHECK (length(authority_bundle_digest) = 64),
  source_narrative_id text NOT NULL,
  source_narrative_attempt_id text NOT NULL,
  rubric_ref text NOT NULL,
  rubric_digest text NOT NULL CHECK (length(rubric_digest) = 64),
  rubric jsonb NOT NULL CHECK (jsonb_typeof(rubric) = 'object'),
  evaluation_case_ref text NOT NULL,
  evaluation_case_digest text NOT NULL CHECK (length(evaluation_case_digest) = 64),
  evaluation_case jsonb NOT NULL CHECK (jsonb_typeof(evaluation_case) = 'object'),
  model_profile_ref text NOT NULL,
  model_profile_digest text NOT NULL CHECK (length(model_profile_digest) = 64),
  model_profile jsonb NOT NULL CHECK (jsonb_typeof(model_profile) = 'object'),
  reviewer_ref text NOT NULL,
  scores jsonb NOT NULL CHECK (jsonb_typeof(scores) = 'object'),
  human_reasons jsonb NOT NULL CHECK (jsonb_typeof(human_reasons) = 'object'),
  result text NOT NULL CHECK (
    result IN ('retain_publication', 'request_independent_narrative_attempt')
  ),
  narrative_attempt_request_ref text,
  advisory boolean NOT NULL CHECK (advisory),
  reviewed_at timestamptz NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_ref, run_attempt_id, evaluation_ref),
  UNIQUE(owner_ref, run_attempt_id, content_digest),
  UNIQUE(
    owner_ref,
    run_attempt_id,
    source_publication_ref,
    evaluation_case_ref,
    model_profile_ref,
    reviewer_ref
  ),
  FOREIGN KEY (owner_ref, run_attempt_id, source_publication_ref)
    REFERENCES waje_runtime.publication_revisions(
      owner_ref, run_attempt_id, publication_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, authority_bundle_ref)
    REFERENCES waje_runtime.authority_bundles(
      owner_ref, run_attempt_id, bundle_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, source_narrative_id)
    REFERENCES waje_runtime.narrative_documents(
      owner_ref, run_attempt_id, narrative_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (owner_ref, run_attempt_id, narrative_attempt_request_ref)
    REFERENCES waje_runtime.narrative_attempt_requests(
      owner_ref, run_attempt_id, request_ref
    ) ON DELETE RESTRICT,
  CHECK (
    (result = 'retain_publication' AND narrative_attempt_request_ref IS NULL)
    OR (
      result = 'request_independent_narrative_attempt'
      AND narrative_attempt_request_ref IS NOT NULL
    )
  )
);

-- v23 repairs development databases where CREATE TABLE IF NOT EXISTS kept the
-- pre-rubric advisory table shape while the migration ledger advanced.
DROP TRIGGER IF EXISTS insight_quality_evaluations_append_only
  ON waje_runtime.insight_quality_evaluations;

ALTER TABLE waje_runtime.insight_quality_evaluations
  ADD COLUMN IF NOT EXISTS rubric_ref text,
  ADD COLUMN IF NOT EXISTS rubric_digest text
    CHECK (length(rubric_digest) = 64),
  ADD COLUMN IF NOT EXISTS rubric jsonb
    CHECK (jsonb_typeof(rubric) = 'object'),
  ADD COLUMN IF NOT EXISTS evaluation_case_digest text
    CHECK (length(evaluation_case_digest) = 64),
  ADD COLUMN IF NOT EXISTS evaluation_case jsonb
    CHECK (jsonb_typeof(evaluation_case) = 'object'),
  ADD COLUMN IF NOT EXISTS model_profile_digest text
    CHECK (length(model_profile_digest) = 64),
  ADD COLUMN IF NOT EXISTS model_profile jsonb
    CHECK (jsonb_typeof(model_profile) = 'object'),
  ADD COLUMN IF NOT EXISTS human_reasons jsonb
    CHECK (jsonb_typeof(human_reasons) = 'object');

UPDATE waje_runtime.insight_quality_evaluations
SET rubric_ref = COALESCE(rubric_ref, payload ->> 'rubric_ref'),
    rubric_digest = COALESCE(rubric_digest, payload ->> 'rubric_digest'),
    rubric = COALESCE(rubric, payload -> 'rubric'),
    evaluation_case_digest = COALESCE(
      evaluation_case_digest,
      payload ->> 'evaluation_case_digest'
    ),
    evaluation_case = COALESCE(evaluation_case, payload -> 'evaluation_case'),
    model_profile_digest = COALESCE(
      model_profile_digest,
      payload ->> 'model_profile_digest'
    ),
    model_profile = COALESCE(model_profile, payload -> 'model_profile'),
    human_reasons = COALESCE(human_reasons, payload -> 'human_reasons');

ALTER TABLE waje_runtime.insight_quality_evaluations
  ALTER COLUMN rubric_ref SET NOT NULL,
  ALTER COLUMN rubric_digest SET NOT NULL,
  ALTER COLUMN rubric SET NOT NULL,
  ALTER COLUMN evaluation_case_digest SET NOT NULL,
  ALTER COLUMN evaluation_case SET NOT NULL,
  ALTER COLUMN model_profile_digest SET NOT NULL,
  ALTER COLUMN model_profile SET NOT NULL,
  ALTER COLUMN human_reasons SET NOT NULL;

CREATE TABLE IF NOT EXISTS waje_runtime.guardrail_promotion_records (
  promotion_ref text PRIMARY KEY,
  governance_scope_ref text NOT NULL,
  evaluation_refs jsonb NOT NULL CHECK (jsonb_typeof(evaluation_refs) = 'array'),
  case_refs jsonb NOT NULL CHECK (jsonb_typeof(case_refs) = 'array'),
  generalizable_pattern_ref text NOT NULL,
  recurrence_evidence_refs jsonb NOT NULL CHECK (jsonb_typeof(recurrence_evidence_refs) = 'array'),
  human_validation_ref text NOT NULL,
  business_owner_ref text NOT NULL,
  system_owner_ref text NOT NULL,
  runtime_guardrail_ref text NOT NULL,
  approved_at timestamptz NOT NULL,
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(governance_scope_ref, promotion_ref),
  UNIQUE(governance_scope_ref, content_digest),
  UNIQUE(governance_scope_ref, runtime_guardrail_ref),
  CHECK (business_owner_ref <> system_owner_ref)
);

CREATE TABLE IF NOT EXISTS waje_runtime.post_seal_failure_terminals (
  terminal_ref text PRIMARY KEY,
  run_attempt_id text NOT NULL
    REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT,
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  supersedes_terminal_ref text,
  status text NOT NULL CHECK (
    status IN ('narrative_failed', 'publication_failed')
  ),
  authority_bundle_ref text NOT NULL
    REFERENCES waje_runtime.authority_bundles(bundle_ref) ON DELETE RESTRICT,
  authority_bundle_digest text NOT NULL CHECK (length(authority_bundle_digest) = 64),
  authority_transition_id text NOT NULL,
  failure_id text NOT NULL,
  lifecycle_state_digest text NOT NULL CHECK (length(lifecycle_state_digest) = 64),
  content_digest text NOT NULL CHECK (length(content_digest) = 64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(run_attempt_id, terminal_ref),
  UNIQUE(run_attempt_id, attempt_number),
  UNIQUE(run_attempt_id, content_digest),
  FOREIGN KEY (run_attempt_id, supersedes_terminal_ref)
    REFERENCES waje_runtime.post_seal_failure_terminals(
      run_attempt_id, terminal_ref
    ) ON DELETE RESTRICT,
  FOREIGN KEY (run_attempt_id, failure_id)
    REFERENCES waje_runtime.failure_records(run_attempt_id, failure_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (run_attempt_id, lifecycle_state_digest)
    REFERENCES waje_runtime.run_lifecycle_state_revisions(
      run_attempt_id, content_digest
    ) ON DELETE RESTRICT,
  CHECK (
    (attempt_number = 1 AND supersedes_terminal_ref IS NULL)
    OR (attempt_number > 1 AND supersedes_terminal_ref IS NOT NULL)
  )
);

-- All records above are authority except delivery_dispatches. Authority records
-- reject UPDATE and DELETE; insertion with an existing ref must be resolved by
-- an exact payload-and-digest comparison in the transaction layer.
DO $$
DECLARE authority_table text;
BEGIN
  FOREACH authority_table IN ARRAY ARRAY[
    'conversation_turns',
    'agent_thread_summaries',
    'agent_generated_artifacts',
    'claim_authority_namespaces',
    'claim_keys',
    'claim_support_edges',
    'claim_revisions',
    'claim_settlement_checkpoints',
    'claim_obligation_settlement_bases',
    'claim_verification_attempts',
    'claim_verification_decisions',
    'local_boundary_authorities',
    'claim_verification_reports',
    'claim_obligation_coverages',
    'claim_graphs',
    'claim_settlements',
    'recommendation_proposals',
    'recommendation_verification_attempts',
    'recommendation_verification_decisions',
    'recommendation_records',
    'authority_bundles',
    'restricted_provider_responses',
    'publication_visibility_policies',
    'public_claim_palettes',
    'public_claims',
    'public_fact_descriptors',
    'public_recommendations',
    'public_limitations',
    'narrative_material_projections',
    'controlled_investigation_operations',
    'narrative_writer_attempts',
    'narrative_documents',
    'narrative_blocks',
    'narrative_fact_bindings',
    'sensitive_output_findings',
    'block_local_validation_reports',
    'block_local_issues',
    'block_verification_attempts',
    'block_vetoes',
    'block_verification_reports',
    'publication_projections',
    'publication_revisions',
    'delivery_outbox_records',
    'publication_customer_payloads',
    'delivery_attempts',
    'customer_publications',
    'narrative_quality_audit_results',
    'narrative_attempt_requests',
    'insight_quality_evaluations',
    'guardrail_promotion_records',
    'post_seal_failure_terminals'
  ]
  LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS %I_append_only ON waje_runtime.%I',
      authority_table,
      authority_table
    );
    EXECUTE format(
      'CREATE TRIGGER %I_append_only BEFORE UPDATE OR DELETE ON waje_runtime.%I '
      'FOR EACH ROW EXECUTE FUNCTION waje_runtime.reject_append_only_authority_mutation()',
      authority_table,
      authority_table
    );
  END LOOP;
END
$$;

CREATE INDEX IF NOT EXISTS idx_claim_revisions_run_key
  ON waje_runtime.claim_revisions(run_attempt_id, claim_key, created_at);
CREATE INDEX IF NOT EXISTS idx_claim_support_edges_target
  ON waje_runtime.claim_support_edges(run_attempt_id, target_claim_key);
CREATE INDEX IF NOT EXISTS idx_narrative_documents_bundle
  ON waje_runtime.narrative_documents(run_attempt_id, authority_bundle_ref, created_at);
CREATE INDEX IF NOT EXISTS idx_delivery_outbox_dispatch
  ON waje_runtime.delivery_dispatches(dispatch_state, next_attempt_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_delivery_attempts_outbox
  ON waje_runtime.delivery_attempts(run_attempt_id, outbox_ref, attempt_number);

-- Tenant isolation is enforced with the authenticated actor stored in the
-- PostgreSQL session. Internal workers explicitly use the reserved `system`
-- scope; customer requests use a transaction-local actor scope.
ALTER TABLE waje_runtime.investigation_threads ENABLE ROW LEVEL SECURITY;
ALTER TABLE waje_runtime.investigation_threads FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS waje_tenant_isolation
  ON waje_runtime.investigation_threads;
CREATE POLICY waje_tenant_isolation
  ON waje_runtime.investigation_threads
  USING (
    current_setting('waje.actor_id', true) = 'system'
    OR owner_id = current_setting('waje.actor_id', true)
  )
  WITH CHECK (
    current_setting('waje.actor_id', true) = 'system'
    OR owner_id = current_setting('waje.actor_id', true)
  );

DO $$
DECLARE
  target record;
  predicate text;
BEGIN
  FOR target IN
    SELECT
      columns.table_name,
      bool_or(columns.column_name = 'thread_id') AS has_thread_id,
      bool_or(columns.column_name = 'run_attempt_id') AS has_run_attempt_id,
      bool_or(columns.column_name = 'run_id') AS has_run_id,
      bool_or(columns.column_name = 'owner_id') AS has_owner_id
    FROM information_schema.columns columns
    WHERE columns.table_schema = 'waje_runtime'
      AND columns.table_name <> 'investigation_threads'
    GROUP BY columns.table_name
    HAVING bool_or(columns.column_name IN (
      'thread_id', 'run_attempt_id', 'run_id', 'owner_id'
    ))
  LOOP
    IF target.has_thread_id THEN
      predicate := format(
        '(current_setting(''waje.actor_id'', true) = ''system'' OR EXISTS ('
        'SELECT 1 FROM waje_runtime.investigation_threads tenant_thread '
        'WHERE tenant_thread.thread_id = %I.thread_id '
        'AND tenant_thread.owner_id = current_setting(''waje.actor_id'', true)))',
        target.table_name
      );
    ELSIF target.has_run_attempt_id THEN
      predicate := format(
        '(current_setting(''waje.actor_id'', true) = ''system'' OR EXISTS ('
        'SELECT 1 FROM waje_runtime.analysis_runs tenant_run '
        'JOIN waje_runtime.investigation_threads tenant_thread '
        'ON tenant_thread.thread_id = tenant_run.thread_id '
        'WHERE tenant_run.run_attempt_id = %I.run_attempt_id '
        'AND tenant_thread.owner_id = current_setting(''waje.actor_id'', true)))',
        target.table_name
      );
    ELSIF target.has_run_id THEN
      predicate := format(
        '(current_setting(''waje.actor_id'', true) = ''system'' OR EXISTS ('
        'SELECT 1 FROM waje_runtime.analysis_runs tenant_run '
        'JOIN waje_runtime.investigation_threads tenant_thread '
        'ON tenant_thread.thread_id = tenant_run.thread_id '
        'WHERE tenant_run.run_id = %I.run_id '
        'AND tenant_thread.owner_id = current_setting(''waje.actor_id'', true)))',
        target.table_name
      );
    ELSIF target.has_owner_id AND target.table_name = 'memory_items' THEN
      predicate := format(
        '(current_setting(''waje.actor_id'', true) = ''system'' '
        'OR %I.owner_id = current_setting(''waje.actor_id'', true))',
        target.table_name
      );
    ELSE
      CONTINUE;
    END IF;

    EXECUTE format(
      'ALTER TABLE waje_runtime.%I ENABLE ROW LEVEL SECURITY',
      target.table_name
    );
    EXECUTE format(
      'ALTER TABLE waje_runtime.%I FORCE ROW LEVEL SECURITY',
      target.table_name
    );
    EXECUTE format(
      'DROP POLICY IF EXISTS waje_tenant_isolation ON waje_runtime.%I',
      target.table_name
    );
    EXECUTE format(
      'CREATE POLICY waje_tenant_isolation ON waje_runtime.%I '
      'USING (%s) WITH CHECK (%s)',
      target.table_name,
      predicate,
      predicate
    );
  END LOOP;
END;
$$;

INSERT INTO waje_runtime.schema_migrations(migration_id, migration_digest)
VALUES (
  'single-authority-workflow.v23',
  '5d63799f71fb49f0c898554573592bb2885059a026f395576de3a04bfa588feb'
)
ON CONFLICT (migration_id) DO NOTHING;
