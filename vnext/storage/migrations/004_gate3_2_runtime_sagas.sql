CREATE TABLE waje_vnext.frame_candidate_records (
    frame_candidate_id text PRIMARY KEY,
    case_id text NOT NULL REFERENCES waje_vnext.investigation_cases(case_id),
    candidate_generation bigint NOT NULL CHECK (candidate_generation > 0),
    prior_frame_candidate_id text NULL
        REFERENCES waje_vnext.frame_candidate_records(frame_candidate_id),
    proposed_frame_revision_id text NOT NULL,
    proposed_frame_content_sha256 char(64) NOT NULL
        CHECK (proposed_frame_content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL,
    UNIQUE (case_id, candidate_generation),
    UNIQUE (proposed_frame_revision_id)
);

CREATE TABLE waje_vnext.active_frame_candidate_heads (
    case_id text PRIMARY KEY REFERENCES waje_vnext.investigation_cases(case_id),
    frame_candidate_id text NOT NULL
        REFERENCES waje_vnext.frame_candidate_records(frame_candidate_id),
    candidate_generation bigint NOT NULL CHECK (candidate_generation > 0),
    proposed_frame_content_sha256 char(64) NOT NULL
        CHECK (proposed_frame_content_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE TABLE waje_vnext.frame_candidate_supersession_records (
    supersession_record_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id),
    frame_candidate_id text NOT NULL UNIQUE
        REFERENCES waje_vnext.frame_candidate_records(frame_candidate_id),
    superseded_by_question_revision_id text NOT NULL
        REFERENCES waje_vnext.question_revisions(question_revision_id),
    authority_epoch bigint NOT NULL CHECK (authority_epoch > 0),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE waje_vnext.objection_closure_records (
    objection_closure_id text PRIMARY KEY,
    objection_id text NOT NULL,
    source_frame_candidate_id text NOT NULL
        REFERENCES waje_vnext.frame_candidate_records(frame_candidate_id),
    replacement_frame_candidate_id text NOT NULL
        REFERENCES waje_vnext.frame_candidate_records(frame_candidate_id),
    payload jsonb NOT NULL,
    UNIQUE (objection_id, replacement_frame_candidate_id),
    CHECK (source_frame_candidate_id <> replacement_frame_candidate_id)
);

CREATE TABLE waje_vnext.frame_review_records (
    frame_review_id text PRIMARY KEY,
    frame_candidate_id text NOT NULL
        REFERENCES waje_vnext.frame_candidate_records(frame_candidate_id),
    reviewer_job_id text NOT NULL,
    disposition text NOT NULL
        CHECK (disposition IN ('accept', 'revise', 'block')),
    reviewed_frame_content_sha256 char(64) NOT NULL
        CHECK (reviewed_frame_content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL,
    UNIQUE (reviewer_job_id),
    UNIQUE (frame_candidate_id)
);

CREATE TABLE waje_vnext.frame_admission_proofs (
    frame_admission_proof_id text PRIMARY KEY,
    case_id text NOT NULL REFERENCES waje_vnext.investigation_cases(case_id),
    frame_candidate_id text NOT NULL
        REFERENCES waje_vnext.frame_candidate_records(frame_candidate_id),
    candidate_generation bigint NOT NULL CHECK (candidate_generation > 0),
    frame_revision_id text NOT NULL UNIQUE,
    frame_content_sha256 char(64) NOT NULL
        CHECK (frame_content_sha256 ~ '^[0-9a-f]{64}$'),
    frame_review_id text NOT NULL UNIQUE
        REFERENCES waje_vnext.frame_review_records(frame_review_id),
    authority_snapshot_sha256 char(64) NOT NULL
        CHECK (authority_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL
);

CREATE TABLE waje_vnext.message_ingress_records (
    ingress_record_id text PRIMARY KEY,
    case_id text NOT NULL REFERENCES waje_vnext.investigation_cases(case_id),
    message_id text NOT NULL REFERENCES waje_vnext.case_mailbox_messages(message_id),
    run_id text NOT NULL,
    authority_epoch bigint NOT NULL CHECK (authority_epoch > 0),
    payload jsonb NOT NULL,
    UNIQUE (message_id)
);

CREATE TABLE waje_vnext.pending_user_messages (
    pending_message_id text PRIMARY KEY,
    ingress_record_id text NOT NULL UNIQUE
        REFERENCES waje_vnext.message_ingress_records(ingress_record_id),
    binding_job_id text NOT NULL UNIQUE,
    payload jsonb NOT NULL
);

CREATE TABLE waje_vnext.message_impact_bindings (
    binding_id text PRIMARY KEY,
    pending_message_id text NOT NULL UNIQUE
        REFERENCES waje_vnext.pending_user_messages(pending_message_id),
    case_id text NOT NULL REFERENCES waje_vnext.investigation_cases(case_id),
    authority_epoch bigint NOT NULL CHECK (authority_epoch > 0),
    disposition text NOT NULL CHECK (
        disposition IN (
            'accepted',
            'needs_user_decision',
            'superseded',
            'rejected'
        )
    ),
    semantic_binding_sha256 char(64) NOT NULL
        CHECK (semantic_binding_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL
);

CREATE TABLE waje_vnext.logical_model_jobs (
    logical_model_job_id text PRIMARY KEY,
    case_id text NOT NULL REFERENCES waje_vnext.investigation_cases(case_id),
    job_id text NOT NULL UNIQUE,
    authority_snapshot_sha256 char(64) NOT NULL
        CHECK (authority_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL
);

CREATE TABLE waje_vnext.provider_attempt_requests (
    provider_attempt_id text PRIMARY KEY,
    logical_model_job_id text NOT NULL
        REFERENCES waje_vnext.logical_model_jobs(logical_model_job_id),
    attempt_number bigint NOT NULL CHECK (attempt_number > 0),
    prior_provider_attempt_id text NULL
        REFERENCES waje_vnext.provider_attempt_requests(provider_attempt_id),
    payload jsonb NOT NULL,
    UNIQUE (logical_model_job_id, attempt_number)
);

CREATE TABLE waje_vnext.provider_attempt_receipts (
    provider_attempt_receipt_id text PRIMARY KEY,
    provider_attempt_id text NOT NULL UNIQUE
        REFERENCES waje_vnext.provider_attempt_requests(provider_attempt_id),
    logical_model_job_id text NOT NULL
        REFERENCES waje_vnext.logical_model_jobs(logical_model_job_id),
    disposition text NOT NULL,
    payload jsonb NOT NULL
);

CREATE TABLE waje_vnext.durable_model_results (
    durable_model_result_id text PRIMARY KEY,
    logical_model_job_id text NOT NULL UNIQUE
        REFERENCES waje_vnext.logical_model_jobs(logical_model_job_id),
    provider_attempt_id text NOT NULL UNIQUE
        REFERENCES waje_vnext.provider_attempt_requests(provider_attempt_id),
    output_sha256 char(64) NOT NULL
        CHECK (output_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL,
    recorded_at timestamptz NOT NULL
);

CREATE TABLE waje_vnext.obligation_schedules (
    schedule_id text PRIMARY KEY,
    case_id text NOT NULL REFERENCES waje_vnext.investigation_cases(case_id),
    frame_revision_id text NOT NULL,
    authority_snapshot_sha256 char(64) NOT NULL
        CHECK (authority_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE waje_vnext.obligation_dispatch_records (
    dispatch_record_id text PRIMARY KEY,
    schedule_id text NOT NULL
        REFERENCES waje_vnext.obligation_schedules(schedule_id),
    obligation_id text NOT NULL,
    outbox_message_id text NOT NULL UNIQUE
        REFERENCES waje_vnext.outbox_messages(outbox_message_id),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (schedule_id, obligation_id)
);

CREATE TABLE waje_vnext.obligation_completion_records (
    completion_record_id text PRIMARY KEY,
    schedule_id text NOT NULL
        REFERENCES waje_vnext.obligation_schedules(schedule_id),
    obligation_id text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (schedule_id, obligation_id)
);

CREATE TABLE waje_vnext.obligation_schedule_checkpoints (
    checkpoint_id text PRIMARY KEY,
    schedule_id text NOT NULL
        REFERENCES waje_vnext.obligation_schedules(schedule_id),
    checkpoint_number bigint NOT NULL CHECK (checkpoint_number > 0),
    prior_checkpoint_id text NULL
        REFERENCES waje_vnext.obligation_schedule_checkpoints(checkpoint_id),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (schedule_id, checkpoint_number)
);

CREATE TABLE waje_vnext.job_disposition_records (
    job_disposition_record_id text PRIMARY KEY,
    outbox_message_id text NOT NULL UNIQUE
        REFERENCES waje_vnext.outbox_messages(outbox_message_id),
    case_id text NOT NULL REFERENCES waje_vnext.investigation_cases(case_id),
    disposition text NOT NULL
        CHECK (disposition IN ('completed', 'superseded', 'terminal_failure')),
    owner_id text NOT NULL,
    fencing_token bigint NULL CHECK (
        fencing_token IS NULL OR fencing_token > 0
    ),
    payload jsonb NOT NULL
);

CREATE TABLE waje_vnext.dispatcher_recovery_cursors (
    dispatcher_id text PRIMARY KEY,
    last_outbox_created_at timestamptz NULL,
    last_source_event_cursor bigint NULL
        CHECK (
            last_source_event_cursor IS NULL
            OR last_source_event_cursor > 0
        ),
    last_outbox_message_id text NULL,
    updated_at timestamptz NOT NULL,
    CHECK (
        (last_outbox_created_at IS NULL)
        = (last_source_event_cursor IS NULL)
        AND (last_source_event_cursor IS NULL)
        = (last_outbox_message_id IS NULL)
    )
);

CREATE TABLE waje_vnext.run_trace_manifests (
    trace_manifest_id text PRIMARY KEY,
    case_id text NOT NULL REFERENCES waje_vnext.investigation_cases(case_id),
    run_id text NOT NULL,
    lineage_sha256 char(64) NOT NULL
        CHECK (lineage_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL
);

CREATE TABLE waje_vnext.measurement_resolution_admissions (
    resolution_outcome_id text PRIMARY KEY
        REFERENCES waje_vnext.measurement_resolution_outcomes(
            resolution_outcome_id
        ) ON DELETE RESTRICT,
    issuer_ref text NOT NULL,
    registry_content_sha256 char(64) NOT NULL
        CHECK (registry_content_sha256 ~ '^[0-9a-f]{64}$'),
    resolver_input_bundle_sha256 char(64) NOT NULL
        CHECK (resolver_input_bundle_sha256 ~ '^[0-9a-f]{64}$'),
    resolution_context_sha256 char(64) NOT NULL
        CHECK (resolution_context_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL
);

CREATE OR REPLACE FUNCTION waje_vnext.reject_immutable_runtime_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'frame_candidate_records',
        'frame_candidate_supersession_records',
        'objection_closure_records',
        'frame_review_records',
        'frame_admission_proofs',
        'message_ingress_records',
        'pending_user_messages',
        'message_impact_bindings',
        'logical_model_jobs',
        'provider_attempt_requests',
        'provider_attempt_receipts',
        'durable_model_results',
        'obligation_schedules',
        'obligation_dispatch_records',
        'obligation_completion_records',
        'obligation_schedule_checkpoints',
        'job_disposition_records',
        'measurement_resolution_admissions',
        'run_trace_manifests'
    ]
    LOOP
        EXECUTE format(
            'CREATE TRIGGER %I_immutable
             BEFORE UPDATE OR DELETE ON waje_vnext.%I
             FOR EACH ROW EXECUTE FUNCTION
             waje_vnext.reject_immutable_runtime_update()',
            table_name,
            table_name
        );
    END LOOP;
END;
$$;
