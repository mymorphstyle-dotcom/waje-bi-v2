ALTER TABLE waje_vnext.outbox_messages
    DROP CONSTRAINT outbox_messages_job_kind_check,
    ADD CONSTRAINT outbox_messages_job_kind_check CHECK (
        job_kind IN (
            'controller_wake',
            'message_binding',
            'primary_agent',
            'semantic_inspection',
            'data_probe',
            'capability',
            'sensitivity',
            'reviewer',
            'projection',
            'obligation'
        )
    );

ALTER TABLE waje_vnext.resolved_evidence_obligations
    DROP CONSTRAINT
        resolved_evidence_obligations_frame_revision_id_evidence_re_key,
    ADD CONSTRAINT
        resolved_evidence_obligations_measurement_slot_key
    UNIQUE (
        frame_revision_id,
        evidence_requirement_id,
        resolution_outcome_id,
        content_sha256
    );

CREATE TABLE waje_vnext.query_binding_envelopes (
    query_binding_id char(64) PRIMARY KEY
        CHECK (query_binding_id ~ '^[0-9a-f]{64}$'),
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id)
        ON DELETE RESTRICT,
    question_revision_id text NOT NULL
        REFERENCES waje_vnext.question_revisions(question_revision_id)
        ON DELETE RESTRICT,
    frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    plan_revision_id text NOT NULL
        REFERENCES waje_vnext.work_plan_revisions(plan_revision_id)
        ON DELETE RESTRICT,
    binding_ordinal bigint NOT NULL CHECK (binding_ordinal > 0),
    task_id text NOT NULL,
    estimand_id text NOT NULL,
    evidence_requirement_id text NOT NULL,
    obligation_id text NOT NULL
        REFERENCES waje_vnext.resolved_evidence_obligations(obligation_id)
        ON DELETE RESTRICT,
    resolution_outcome_id text NOT NULL
        REFERENCES waje_vnext.measurement_resolution_outcomes(
            resolution_outcome_id
        ) ON DELETE RESTRICT,
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (plan_revision_id, obligation_id),
    UNIQUE (plan_revision_id, binding_ordinal)
);

CREATE TABLE waje_vnext.plan_adoption_records (
    plan_adoption_id char(64) PRIMARY KEY
        CHECK (plan_adoption_id ~ '^[0-9a-f]{64}$'),
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id)
        ON DELETE RESTRICT,
    question_revision_id text NOT NULL
        REFERENCES waje_vnext.question_revisions(question_revision_id)
        ON DELETE RESTRICT,
    frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    plan_revision_id text NOT NULL UNIQUE
        REFERENCES waje_vnext.work_plan_revisions(plan_revision_id)
        ON DELETE RESTRICT,
    expected_head_version bigint NOT NULL CHECK (expected_head_version >= 0),
    authority_snapshot_sha256 char(64) NOT NULL
        CHECK (authority_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    derivation_proof_sha256 char(64) NOT NULL
        CHECK (derivation_proof_sha256 ~ '^[0-9a-f]{64}$'),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3)
);

ALTER TABLE waje_vnext.investigation_cases
    DROP CONSTRAINT investigation_case_plan_head_fk,
    ADD CONSTRAINT investigation_case_plan_adoption_head_fk
    FOREIGN KEY (accepted_plan_revision_id)
    REFERENCES waje_vnext.plan_adoption_records(plan_revision_id)
    ON DELETE RESTRICT;

CREATE TABLE waje_vnext.conformance_execution_specs (
    conformance_execution_spec_id char(64) PRIMARY KEY
        CHECK (conformance_execution_spec_id ~ '^[0-9a-f]{64}$'),
    logical_execution_id char(64) NOT NULL UNIQUE
        CHECK (logical_execution_id ~ '^[0-9a-f]{64}$'),
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id)
        ON DELETE RESTRICT,
    frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    plan_revision_id text NOT NULL
        REFERENCES waje_vnext.plan_adoption_records(plan_revision_id)
        ON DELETE RESTRICT,
    task_id text NOT NULL,
    obligation_id text NOT NULL
        REFERENCES waje_vnext.resolved_evidence_obligations(obligation_id)
        ON DELETE RESTRICT,
    query_binding_id char(64) NOT NULL UNIQUE
        REFERENCES waje_vnext.query_binding_envelopes(query_binding_id)
        ON DELETE RESTRICT,
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3)
);

CREATE TABLE waje_vnext.logical_execution_attempts (
    logical_execution_attempt_id char(64) PRIMARY KEY
        CHECK (logical_execution_attempt_id ~ '^[0-9a-f]{64}$'),
    logical_execution_id char(64) NOT NULL
        REFERENCES waje_vnext.conformance_execution_specs(
            logical_execution_id
        ) ON DELETE RESTRICT,
    conformance_execution_spec_id char(64) NOT NULL
        REFERENCES waje_vnext.conformance_execution_specs(
            conformance_execution_spec_id
        ) ON DELETE RESTRICT,
    query_binding_id char(64) NOT NULL
        REFERENCES waje_vnext.query_binding_envelopes(query_binding_id)
        ON DELETE RESTRICT,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id)
        ON DELETE RESTRICT,
    frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    plan_revision_id text NOT NULL
        REFERENCES waje_vnext.plan_adoption_records(plan_revision_id)
        ON DELETE RESTRICT,
    task_id text NOT NULL,
    attempt_number bigint NOT NULL CHECK (attempt_number > 0),
    prior_attempt_id char(64) NULL
        REFERENCES waje_vnext.logical_execution_attempts(
            logical_execution_attempt_id
        ) ON DELETE RESTRICT,
    attempt_kind text NOT NULL
        CHECK (attempt_kind IN ('initial', 'technical_retry')),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    requested_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (logical_execution_id, attempt_number),
    CHECK (
        (
            attempt_number = 1
            AND prior_attempt_id IS NULL
            AND attempt_kind = 'initial'
        )
        OR (
            attempt_number > 1
            AND prior_attempt_id IS NOT NULL
            AND attempt_kind = 'technical_retry'
        )
    )
);

CREATE TRIGGER query_binding_envelopes_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.query_binding_envelopes
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();

CREATE TRIGGER plan_adoption_records_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.plan_adoption_records
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();

CREATE TRIGGER conformance_execution_specs_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.conformance_execution_specs
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();

CREATE TRIGGER logical_execution_attempts_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.logical_execution_attempts
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
