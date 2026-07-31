-- Gate 3.1 establishes schema epoch 3. Existing epoch-1/2 authority rows
-- cannot be interpreted as typed measurement authority.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM waje_vnext.investigation_cases LIMIT 1)
       OR EXISTS (SELECT 1 FROM waje_vnext.analysis_frame_revisions LIMIT 1)
       OR EXISTS (SELECT 1 FROM waje_vnext.work_plan_revisions LIMIT 1)
       OR EXISTS (SELECT 1 FROM waje_vnext.evidence_records LIMIT 1)
       OR EXISTS (SELECT 1 FROM waje_vnext.answer_versions LIMIT 1)
    THEN
        RAISE EXCEPTION
            'G3.1 schema epoch 3 requires a clean waje_vnext authority schema; reset the development database and reapply migrations'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

ALTER TABLE waje_vnext.investigation_cases
    ADD COLUMN accepted_question_revision_id text,
    ADD COLUMN analysis_cycle_id text NOT NULL
        DEFAULT 'schema-epoch-3-uninitialized';

ALTER TABLE waje_vnext.investigation_cases
    ALTER COLUMN analysis_cycle_id DROP DEFAULT,
    ADD CONSTRAINT investigation_case_question_before_frame CHECK (
        accepted_frame_revision_id IS NULL
        OR accepted_question_revision_id IS NOT NULL
    );

CREATE TABLE waje_vnext.question_revisions (
    question_revision_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    revision_number bigint NOT NULL CHECK (revision_number >= 1),
    prior_question_revision_id text
        REFERENCES waje_vnext.question_revisions(question_revision_id)
        ON DELETE RESTRICT,
    analysis_cycle_id text NOT NULL,
    accepted_head_version bigint NOT NULL CHECK (accepted_head_version >= 1),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    schema_epoch bigint GENERATED ALWAYS AS (
        (payload->>'schema_epoch')::bigint
    ) STORED,
    UNIQUE (case_id, revision_number),
    UNIQUE (case_id, accepted_head_version),
    CHECK (schema_epoch = 3),
    CHECK (
        (revision_number = 1 AND prior_question_revision_id IS NULL)
        OR (revision_number > 1 AND prior_question_revision_id IS NOT NULL)
    )
);

ALTER TABLE waje_vnext.investigation_cases
    ADD CONSTRAINT investigation_case_question_head_fk
    FOREIGN KEY (accepted_question_revision_id)
    REFERENCES waje_vnext.question_revisions(question_revision_id)
    ON DELETE RESTRICT;

ALTER TABLE waje_vnext.analysis_frame_revisions
    ADD COLUMN question_revision_id text NOT NULL
        REFERENCES waje_vnext.question_revisions(question_revision_id)
        ON DELETE RESTRICT,
    ADD COLUMN schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    ADD COLUMN identity_algorithm_version text NOT NULL CHECK (
        identity_algorithm_version = 'measurement-identity.v1'
    ),
    ADD COLUMN semantic_measurement_ids text[] NOT NULL CHECK (
        cardinality(semantic_measurement_ids) >= 1
    ),
    ADD COLUMN authority_binding_ids text[] NOT NULL CHECK (
        cardinality(authority_binding_ids)
        = cardinality(semantic_measurement_ids)
    );

ALTER TABLE waje_vnext.answer_versions
    DROP CONSTRAINT answer_versions_status_check,
    ADD CONSTRAINT answer_versions_gate3_provisional_only
    CHECK (
        status = 'provisional'
        AND payload->>'status' = 'provisional'
    );

CREATE TABLE waje_vnext.measurement_resolution_outcomes (
    resolution_outcome_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    question_revision_id text NOT NULL
        REFERENCES waje_vnext.question_revisions(question_revision_id)
        ON DELETE RESTRICT,
    frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    estimand_id text NOT NULL,
    semantic_measurement_id text NOT NULL CHECK (
        semantic_measurement_id ~ '^[0-9a-f]{64}$'
    ),
    authority_binding_id text NOT NULL CHECK (
        authority_binding_id ~ '^[0-9a-f]{64}$'
    ),
    outcome_kind text NOT NULL CHECK (
        outcome_kind IN (
            'resolved_instance',
            'typed_resolution_boundary'
        )
    ),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (
        frame_revision_id,
        estimand_id,
        content_sha256
    )
);

CREATE TABLE waje_vnext.resolved_evidence_obligations (
    obligation_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    estimand_id text NOT NULL,
    evidence_requirement_id text NOT NULL,
    resolution_outcome_id text NOT NULL
        REFERENCES waje_vnext.measurement_resolution_outcomes(
            resolution_outcome_id
        ) ON DELETE RESTRICT,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (
        frame_revision_id,
        evidence_requirement_id,
        resolution_outcome_id
    )
);

CREATE TABLE waje_vnext.evidence_validity_records (
    evidence_validity_record_id text PRIMARY KEY,
    evidence_record_id text NOT NULL
        REFERENCES waje_vnext.evidence_records(evidence_record_id)
        ON DELETE RESTRICT,
    prior_validity_record_id text
        REFERENCES waje_vnext.evidence_validity_records(
            evidence_validity_record_id
        ) ON DELETE RESTRICT,
    disposition_status text NOT NULL CHECK (
        disposition_status IN (
            'admitted_valid',
            'never_admitted',
            'superseded',
            'revoked'
        )
    ),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3)
);

CREATE UNIQUE INDEX evidence_validity_one_root
    ON waje_vnext.evidence_validity_records (evidence_record_id)
    WHERE prior_validity_record_id IS NULL;

CREATE UNIQUE INDEX evidence_validity_one_successor
    ON waje_vnext.evidence_validity_records (prior_validity_record_id)
    WHERE prior_validity_record_id IS NOT NULL;

CREATE TABLE waje_vnext.obligation_satisfaction_records (
    satisfaction_record_id text PRIMARY KEY,
    obligation_id text NOT NULL
        REFERENCES waje_vnext.resolved_evidence_obligations(obligation_id)
        ON DELETE RESTRICT,
    satisfaction_status text NOT NULL CHECK (
        satisfaction_status IN (
            'open',
            'satisfied',
            'boundary',
            'blocked',
            'superseded'
        )
    ),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3)
);

CREATE TABLE waje_vnext.settlement_precondition_reports (
    settlement_precondition_report_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    question_revision_id text NOT NULL
        REFERENCES waje_vnext.question_revisions(question_revision_id)
        ON DELETE RESTRICT,
    frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    plan_revision_id text NOT NULL
        REFERENCES waje_vnext.work_plan_revisions(plan_revision_id)
        ON DELETE RESTRICT,
    precondition_status text NOT NULL CHECK (
        precondition_status IN (
            'eligible_for_future_settlement',
            'blocked'
        )
    ),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3)
);

CREATE TRIGGER question_revisions_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.question_revisions
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER measurement_resolution_outcomes_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.measurement_resolution_outcomes
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER resolved_evidence_obligations_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.resolved_evidence_obligations
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER evidence_validity_records_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.evidence_validity_records
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER obligation_satisfaction_records_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.obligation_satisfaction_records
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER settlement_precondition_reports_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.settlement_precondition_reports
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
