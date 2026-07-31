-- Gate 3.5 replaces the development-only evidence, answer, and projection
-- persistence contracts.  No compatibility interpretation exists for the
-- Gate 1 / Gate 3.1 placeholder rows.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM waje_vnext.evidence_records LIMIT 1)
       OR EXISTS (
            SELECT 1
            FROM waje_vnext.evidence_validity_records
            LIMIT 1
       )
       OR EXISTS (
            SELECT 1
            FROM waje_vnext.obligation_satisfaction_records
            LIMIT 1
       )
       OR EXISTS (SELECT 1 FROM waje_vnext.answer_versions LIMIT 1)
       OR EXISTS (
            SELECT 1
            FROM waje_vnext.settlement_precondition_reports
            LIMIT 1
       )
       OR EXISTS (SELECT 1 FROM waje_vnext.reviewer_objections LIMIT 1)
    THEN
        RAISE EXCEPTION
            'G3.5 evidence/answer/workflow authority requires empty superseded evidence, answer, validity, satisfaction, settlement, and reviewer tables; reset the disposable development database and reapply migrations'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

ALTER TABLE waje_vnext.investigation_cases
    DROP CONSTRAINT investigation_case_answer_head_fk;

DROP TABLE waje_vnext.reviewer_objections;
DROP TABLE waje_vnext.settlement_precondition_reports;
DROP TABLE waje_vnext.obligation_satisfaction_records;
DROP TABLE waje_vnext.evidence_validity_records;
DROP TABLE waje_vnext.answer_versions;
DROP TABLE waje_vnext.evidence_records;

-- Composite authority keys let every downstream row prove that IDs, hashes,
-- and the accepted business identity came from the same authority object.
ALTER TABLE waje_vnext.plan_adoption_records
    ADD CONSTRAINT plan_adoption_g35_authority_key UNIQUE (
        plan_adoption_id,
        plan_revision_id,
        question_revision_id,
        frame_revision_id,
        content_sha256
    ),
    ADD CONSTRAINT plan_adoption_g35_answer_key UNIQUE (
        plan_adoption_id,
        plan_revision_id,
        question_revision_id,
        frame_revision_id
    ),
    ADD CONSTRAINT plan_adoption_g35_projection_key UNIQUE (
        plan_adoption_id,
        plan_revision_id,
        content_sha256
    );

ALTER TABLE waje_vnext.work_plan_revisions
    ADD CONSTRAINT work_plan_g35_projection_key UNIQUE (
        plan_revision_id,
        content_sha256
    );

ALTER TABLE waje_vnext.question_revisions
    ADD CONSTRAINT question_revision_g35_projection_key UNIQUE (
        question_revision_id,
        content_sha256
    );

ALTER TABLE waje_vnext.analysis_frame_revisions
    ADD CONSTRAINT analysis_frame_g35_projection_key UNIQUE (
        frame_revision_id,
        content_sha256
    );

ALTER TABLE waje_vnext.query_binding_envelopes
    ADD CONSTRAINT query_binding_g35_authority_key UNIQUE (
        query_binding_id,
        plan_revision_id,
        obligation_id,
        resolution_outcome_id,
        content_sha256
    );

ALTER TABLE waje_vnext.measurement_resolution_outcomes
    ADD CONSTRAINT resolution_outcome_g35_authority_key UNIQUE (
        resolution_outcome_id,
        frame_revision_id,
        estimand_id,
        content_sha256
    );

ALTER TABLE waje_vnext.resolved_evidence_obligations
    ADD CONSTRAINT evidence_obligation_g35_authority_key UNIQUE (
        obligation_id,
        frame_revision_id,
        estimand_id,
        evidence_requirement_id,
        resolution_outcome_id,
        content_sha256
    );

ALTER TABLE waje_vnext.event_journal
    ADD CONSTRAINT event_journal_g35_projection_source_key UNIQUE (
        case_id,
        cursor,
        event_id
    );

CREATE TABLE waje_vnext.evidence_records (
    evidence_record_id char(64) PRIMARY KEY
        CHECK (evidence_record_id ~ '^[0-9a-f]{64}$'),
    run_id text NOT NULL,
    profile text NOT NULL CHECK (
        profile IN ('conformance', 'production')
    ),
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
        REFERENCES waje_vnext.plan_adoption_records(plan_revision_id)
        ON DELETE RESTRICT,
    task_id text NOT NULL,
    estimand_id text NOT NULL,
    evidence_requirement_id text NOT NULL,
    obligation_id text NOT NULL,
    obligation_content_sha256 char(64) NOT NULL
        CHECK (obligation_content_sha256 ~ '^[0-9a-f]{64}$'),
    resolution_outcome_id text NOT NULL,
    resolution_outcome_content_sha256 char(64) NOT NULL
        CHECK (
            resolution_outcome_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    resolution_id text NOT NULL,
    semantic_measurement_id char(64) NOT NULL
        CHECK (semantic_measurement_id ~ '^[0-9a-f]{64}$'),
    authority_binding_id char(64) NOT NULL
        CHECK (authority_binding_id ~ '^[0-9a-f]{64}$'),
    query_binding_id char(64) NOT NULL,
    query_binding_content_sha256 char(64) NOT NULL
        CHECK (query_binding_content_sha256 ~ '^[0-9a-f]{64}$'),
    logical_execution_id char(64) NOT NULL
        CHECK (logical_execution_id ~ '^[0-9a-f]{64}$'),
    provenance_kind text NOT NULL CHECK (
        provenance_kind IN ('conformance', 'physical_query')
    ),
    conformance_execution_spec_id char(64),
    conformance_logical_execution_attempt_id char(64),
    physical_query_spec_id char(64),
    physical_provider_receipt_id char(64),
    result_material_kind text NOT NULL CHECK (
        result_material_kind IN ('inline', 'stable_handle')
    ),
    result_material_content_sha256 char(64) NOT NULL
        CHECK (
            result_material_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    result_handle_id char(64),
    evidence_strength text NOT NULL CHECK (
        evidence_strength IN (
            'boundary_only',
            'descriptive',
            'accounting',
            'associational',
            'causal'
        )
    ),
    data_contract_version_ref text NOT NULL,
    snapshot_release_ref text NOT NULL,
    coverage_watermark_ref text NOT NULL,
    timezone text NOT NULL,
    business_day_cutoff text NOT NULL,
    identity_version text NOT NULL CHECK (
        identity_version = 'evidence-identity.g3.5.v1'
    ),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    produced_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (
        evidence_record_id,
        profile,
        content_sha256
    ),
    UNIQUE (evidence_record_id, content_sha256),
    UNIQUE (
        query_binding_id,
        logical_execution_id,
        result_material_content_sha256
    ),
    FOREIGN KEY (
        resolution_outcome_id,
        frame_revision_id,
        estimand_id,
        resolution_outcome_content_sha256
    ) REFERENCES waje_vnext.measurement_resolution_outcomes (
        resolution_outcome_id,
        frame_revision_id,
        estimand_id,
        content_sha256
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        obligation_id,
        frame_revision_id,
        estimand_id,
        evidence_requirement_id,
        resolution_outcome_id,
        obligation_content_sha256
    ) REFERENCES waje_vnext.resolved_evidence_obligations (
        obligation_id,
        frame_revision_id,
        estimand_id,
        evidence_requirement_id,
        resolution_outcome_id,
        content_sha256
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        query_binding_id,
        plan_revision_id,
        obligation_id,
        resolution_outcome_id,
        query_binding_content_sha256
    ) REFERENCES waje_vnext.query_binding_envelopes (
        query_binding_id,
        plan_revision_id,
        obligation_id,
        resolution_outcome_id,
        content_sha256
    ) ON DELETE RESTRICT,
    FOREIGN KEY (conformance_execution_spec_id)
        REFERENCES waje_vnext.conformance_execution_specs(
            conformance_execution_spec_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (conformance_logical_execution_attempt_id)
        REFERENCES waje_vnext.logical_execution_attempts(
            logical_execution_attempt_id
        ) ON DELETE RESTRICT,
    CHECK (
        (
            profile = 'conformance'
            AND provenance_kind = 'conformance'
            AND conformance_execution_spec_id IS NOT NULL
            AND conformance_logical_execution_attempt_id IS NOT NULL
            AND physical_query_spec_id IS NULL
            AND physical_provider_receipt_id IS NULL
        )
        OR (
            profile = 'production'
            AND provenance_kind = 'physical_query'
            AND conformance_execution_spec_id IS NULL
            AND conformance_logical_execution_attempt_id IS NULL
            AND physical_query_spec_id IS NOT NULL
            AND physical_query_spec_id ~ '^[0-9a-f]{64}$'
            AND physical_provider_receipt_id IS NOT NULL
            AND physical_provider_receipt_id ~ '^[0-9a-f]{64}$'
        )
    ),
    CHECK (
        (
            result_material_kind = 'inline'
            AND result_handle_id IS NULL
        )
        OR (
            result_material_kind = 'stable_handle'
            AND result_handle_id IS NOT NULL
            AND result_handle_id ~ '^[0-9a-f]{64}$'
        )
    )
);

CREATE TABLE waje_vnext.capability_result_envelopes (
    capability_result_envelope_id char(64) PRIMARY KEY
        CHECK (
            capability_result_envelope_id ~ '^[0-9a-f]{64}$'
        ),
    profile text NOT NULL CHECK (
        profile IN ('conformance', 'production')
    ),
    provenance_kind text NOT NULL CHECK (
        provenance_kind IN ('conformance', 'physical_query')
    ),
    result_material_kind text NOT NULL CHECK (
        result_material_kind IN ('inline', 'stable_handle')
    ),
    run_id text NOT NULL,
    schedule_id text NOT NULL
        REFERENCES waje_vnext.obligation_schedules(schedule_id)
        ON DELETE RESTRICT,
    dispatch_record_id text NOT NULL UNIQUE
        REFERENCES waje_vnext.obligation_dispatch_records(
            dispatch_record_id
        ) ON DELETE RESTRICT,
    outbox_message_id text NOT NULL UNIQUE
        REFERENCES waje_vnext.outbox_messages(outbox_message_id)
        ON DELETE RESTRICT,
    logical_execution_attempt_id char(64) NOT NULL
        CHECK (
            logical_execution_attempt_id ~ '^[0-9a-f]{64}$'
        ),
    logical_execution_attempt_content_sha256 char(64) NOT NULL
        CHECK (
            logical_execution_attempt_content_sha256
            ~ '^[0-9a-f]{64}$'
        ),
    capability_invocation_id char(64) NOT NULL UNIQUE
        CHECK (capability_invocation_id ~ '^[0-9a-f]{64}$'),
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
    query_binding_id char(64) NOT NULL
        REFERENCES waje_vnext.query_binding_envelopes(query_binding_id)
        ON DELETE RESTRICT,
    query_binding_content_sha256 char(64) NOT NULL
        CHECK (query_binding_content_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_record_id char(64) NOT NULL UNIQUE,
    evidence_record_content_sha256 char(64) NOT NULL
        CHECK (
            evidence_record_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    execution_provenance_content_sha256 char(64) NOT NULL
        CHECK (
            execution_provenance_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    result_material_content_sha256 char(64) NOT NULL
        CHECK (
            result_material_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    produced_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (
        capability_result_envelope_id,
        profile,
        content_sha256
    ),
    UNIQUE (
        capability_result_envelope_id,
        profile,
        evidence_record_id,
        evidence_record_content_sha256,
        content_sha256
    ),
    FOREIGN KEY (
        evidence_record_id,
        profile,
        evidence_record_content_sha256
    ) REFERENCES waje_vnext.evidence_records (
        evidence_record_id,
        profile,
        content_sha256
    ) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (profile = 'conformance' AND provenance_kind = 'conformance')
        OR (
            profile = 'production'
            AND provenance_kind = 'physical_query'
        )
    )
);

CREATE TABLE waje_vnext.capability_result_receipts (
    capability_result_receipt_id char(64) PRIMARY KEY
        CHECK (
            capability_result_receipt_id ~ '^[0-9a-f]{64}$'
        ),
    profile text NOT NULL CHECK (
        profile IN ('conformance', 'production')
    ),
    run_id text NOT NULL,
    schedule_id text NOT NULL
        REFERENCES waje_vnext.obligation_schedules(schedule_id)
        ON DELETE RESTRICT,
    dispatch_record_id text NOT NULL UNIQUE
        REFERENCES waje_vnext.obligation_dispatch_records(
            dispatch_record_id
        ) ON DELETE RESTRICT,
    outbox_message_id text NOT NULL UNIQUE
        REFERENCES waje_vnext.outbox_messages(outbox_message_id)
        ON DELETE RESTRICT,
    delivery_owner_id text NOT NULL,
    delivery_fencing_token bigint NOT NULL CHECK (
        delivery_fencing_token >= 1
    ),
    logical_execution_attempt_id char(64) NOT NULL
        CHECK (
            logical_execution_attempt_id ~ '^[0-9a-f]{64}$'
        ),
    logical_execution_attempt_content_sha256 char(64) NOT NULL
        CHECK (
            logical_execution_attempt_content_sha256
            ~ '^[0-9a-f]{64}$'
        ),
    capability_result_envelope_id char(64) NOT NULL UNIQUE,
    capability_result_envelope_content_sha256 char(64) NOT NULL
        CHECK (
            capability_result_envelope_content_sha256
            ~ '^[0-9a-f]{64}$'
        ),
    capability_invocation_id char(64) NOT NULL UNIQUE
        CHECK (capability_invocation_id ~ '^[0-9a-f]{64}$'),
    query_binding_id char(64) NOT NULL
        REFERENCES waje_vnext.query_binding_envelopes(query_binding_id)
        ON DELETE RESTRICT,
    execution_provenance_content_sha256 char(64) NOT NULL
        CHECK (
            execution_provenance_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    result_material_content_sha256 char(64) NOT NULL
        CHECK (
            result_material_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    operation_id text NOT NULL,
    idempotency_key text NOT NULL,
    causation_id text NOT NULL,
    correlation_id text NOT NULL,
    authority_revision bigint NOT NULL CHECK (authority_revision >= 0),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    received_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (profile, capability_invocation_id),
    UNIQUE (outbox_message_id, idempotency_key),
    UNIQUE (
        capability_result_receipt_id,
        profile,
        capability_result_envelope_id,
        capability_result_envelope_content_sha256,
        content_sha256
    ),
    FOREIGN KEY (
        capability_result_envelope_id,
        profile,
        capability_result_envelope_content_sha256
    ) REFERENCES waje_vnext.capability_result_envelopes (
        capability_result_envelope_id,
        profile,
        content_sha256
    ) ON DELETE RESTRICT,
    CHECK (causation_id = outbox_message_id),
    CHECK (correlation_id = run_id)
);

CREATE TABLE waje_vnext.evidence_admission_records (
    evidence_admission_id char(64) PRIMARY KEY
        CHECK (evidence_admission_id ~ '^[0-9a-f]{64}$'),
    profile text NOT NULL CHECK (
        profile IN ('conformance', 'production')
    ),
    admission_status text NOT NULL CHECK (
        admission_status IN ('accepted', 'rejected')
    ),
    evidence_record_id char(64) NOT NULL UNIQUE,
    evidence_record_content_sha256 char(64) NOT NULL
        CHECK (
            evidence_record_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    capability_result_envelope_id char(64) NOT NULL UNIQUE,
    capability_result_envelope_content_sha256 char(64) NOT NULL
        CHECK (
            capability_result_envelope_content_sha256
            ~ '^[0-9a-f]{64}$'
        ),
    capability_result_receipt_id char(64) NOT NULL UNIQUE,
    capability_result_receipt_content_sha256 char(64) NOT NULL
        CHECK (
            capability_result_receipt_content_sha256
            ~ '^[0-9a-f]{64}$'
        ),
    obligation_id text NOT NULL
        REFERENCES waje_vnext.resolved_evidence_obligations(obligation_id)
        ON DELETE RESTRICT,
    obligation_content_sha256 char(64) NOT NULL
        CHECK (obligation_content_sha256 ~ '^[0-9a-f]{64}$'),
    query_binding_id char(64) NOT NULL
        REFERENCES waje_vnext.query_binding_envelopes(query_binding_id)
        ON DELETE RESTRICT,
    query_binding_content_sha256 char(64) NOT NULL
        CHECK (query_binding_content_sha256 ~ '^[0-9a-f]{64}$'),
    plan_adoption_id char(64) NOT NULL
        REFERENCES waje_vnext.plan_adoption_records(plan_adoption_id)
        ON DELETE RESTRICT,
    plan_adoption_content_sha256 char(64) NOT NULL
        CHECK (plan_adoption_content_sha256 ~ '^[0-9a-f]{64}$'),
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id)
        ON DELETE RESTRICT,
    accepted_question_revision_id text NOT NULL
        REFERENCES waje_vnext.question_revisions(question_revision_id)
        ON DELETE RESTRICT,
    accepted_frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    accepted_plan_revision_id text NOT NULL
        REFERENCES waje_vnext.plan_adoption_records(plan_revision_id)
        ON DELETE RESTRICT,
    accepted_head_version bigint NOT NULL CHECK (
        accepted_head_version >= 0
    ),
    mailbox_authority_epoch bigint NOT NULL CHECK (
        mailbox_authority_epoch >= 0
    ),
    authority_fence_content_sha256 char(64) NOT NULL
        CHECK (
            authority_fence_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    authority_snapshot_content_sha256 char(64) NOT NULL
        CHECK (
            authority_snapshot_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    scope_relation text NOT NULL CHECK (
        scope_relation IN (
            'exact',
            'subset',
            'superset',
            'lawful_projection',
            'lawful_aggregation',
            'disjoint',
            'unknown'
        )
    ),
    window_proof_sha256 char(64) NOT NULL
        CHECK (window_proof_sha256 ~ '^[0-9a-f]{64}$'),
    exposure_proof_sha256 char(64) NOT NULL
        CHECK (exposure_proof_sha256 ~ '^[0-9a-f]{64}$'),
    unit_proof_sha256 char(64) NOT NULL
        CHECK (unit_proof_sha256 ~ '^[0-9a-f]{64}$'),
    grain_proof_sha256 char(64) NOT NULL
        CHECK (grain_proof_sha256 ~ '^[0-9a-f]{64}$'),
    data_version_proof_sha256 char(64) NOT NULL
        CHECK (data_version_proof_sha256 ~ '^[0-9a-f]{64}$'),
    effective_strength text NOT NULL CHECK (
        effective_strength IN (
            'boundary_only',
            'descriptive',
            'accounting',
            'associational',
            'causal'
        )
    ),
    policy_version text NOT NULL CHECK (
        policy_version = 'evidence-admission.g3.5.v1'
    ),
    derived_input_sha256 char(64) NOT NULL
        CHECK (derived_input_sha256 ~ '^[0-9a-f]{64}$'),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    admitted_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (
        evidence_admission_id,
        evidence_record_id,
        profile,
        content_sha256
    ),
    UNIQUE (
        evidence_admission_id,
        evidence_record_id,
        content_sha256
    ),
    UNIQUE (
        evidence_record_id,
        evidence_admission_id,
        content_sha256
    ),
    FOREIGN KEY (
        evidence_record_id,
        profile,
        evidence_record_content_sha256
    ) REFERENCES waje_vnext.evidence_records (
        evidence_record_id,
        profile,
        content_sha256
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        capability_result_envelope_id,
        profile,
        evidence_record_id,
        evidence_record_content_sha256,
        capability_result_envelope_content_sha256
    ) REFERENCES waje_vnext.capability_result_envelopes (
        capability_result_envelope_id,
        profile,
        evidence_record_id,
        evidence_record_content_sha256,
        content_sha256
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        capability_result_receipt_id,
        profile,
        capability_result_envelope_id,
        capability_result_envelope_content_sha256,
        capability_result_receipt_content_sha256
    ) REFERENCES waje_vnext.capability_result_receipts (
        capability_result_receipt_id,
        profile,
        capability_result_envelope_id,
        capability_result_envelope_content_sha256,
        content_sha256
    ) ON DELETE RESTRICT,
    CHECK (
        profile <> 'production'
        OR admission_status = 'rejected'
    ),
    CHECK (
        admission_status = 'rejected'
        OR scope_relation IN (
            'exact',
            'superset',
            'lawful_projection',
            'lawful_aggregation'
        )
    )
);

CREATE TABLE waje_vnext.evidence_validity_records (
    evidence_validity_id char(64) PRIMARY KEY
        CHECK (evidence_validity_id ~ '^[0-9a-f]{64}$'),
    evidence_record_id char(64) NOT NULL
        REFERENCES waje_vnext.evidence_records(evidence_record_id)
        ON DELETE RESTRICT,
    evidence_admission_id char(64) NOT NULL,
    evidence_admission_content_sha256 char(64) NOT NULL
        CHECK (
            evidence_admission_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    prior_evidence_validity_id char(64),
    prior_evidence_validity_content_sha256 char(64),
    validity_status text NOT NULL CHECK (
        validity_status IN (
            'admitted_valid',
            'never_admitted',
            'superseded',
            'revoked'
        )
    ),
    reason_code text NOT NULL,
    policy_version text NOT NULL CHECK (
        policy_version = 'evidence-validity.g3.5.v1'
    ),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    recorded_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (
        evidence_validity_id,
        evidence_record_id,
        content_sha256
    ),
    FOREIGN KEY (
        evidence_admission_id,
        evidence_record_id,
        evidence_admission_content_sha256
    ) REFERENCES waje_vnext.evidence_admission_records (
        evidence_admission_id,
        evidence_record_id,
        content_sha256
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        prior_evidence_validity_id,
        evidence_record_id,
        prior_evidence_validity_content_sha256
    ) REFERENCES waje_vnext.evidence_validity_records (
        evidence_validity_id,
        evidence_record_id,
        content_sha256
    ) ON DELETE RESTRICT,
    CHECK (
        (
            validity_status IN ('admitted_valid', 'never_admitted')
            AND prior_evidence_validity_id IS NULL
            AND prior_evidence_validity_content_sha256 IS NULL
        )
        OR (
            validity_status IN ('superseded', 'revoked')
            AND prior_evidence_validity_id IS NOT NULL
            AND prior_evidence_validity_content_sha256 IS NOT NULL
        )
    )
);

CREATE UNIQUE INDEX evidence_validity_one_root
    ON waje_vnext.evidence_validity_records(evidence_record_id)
    WHERE prior_evidence_validity_id IS NULL;

CREATE UNIQUE INDEX evidence_validity_one_successor
    ON waje_vnext.evidence_validity_records(
        prior_evidence_validity_id
    )
    WHERE prior_evidence_validity_id IS NOT NULL;

CREATE TABLE waje_vnext.provisional_answer_candidates (
    answer_candidate_id char(64) PRIMARY KEY
        CHECK (answer_candidate_id ~ '^[0-9a-f]{64}$'),
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id)
        ON DELETE RESTRICT,
    question_revision_id text NOT NULL
        REFERENCES waje_vnext.question_revisions(question_revision_id)
        ON DELETE RESTRICT,
    frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    plan_revision_id text NOT NULL,
    plan_adoption_id char(64) NOT NULL,
    plan_adoption_content_sha256 char(64) NOT NULL
        CHECK (plan_adoption_content_sha256 ~ '^[0-9a-f]{64}$'),
    authority_snapshot_content_sha256 char(64) NOT NULL
        CHECK (
            authority_snapshot_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    accepted_head_version bigint NOT NULL CHECK (
        accepted_head_version >= 0
    ),
    version_number bigint NOT NULL CHECK (version_number >= 1),
    prior_answer_version_id text,
    created_by_action_id text NOT NULL
        REFERENCES waje_vnext.action_records(action_id)
        ON DELETE RESTRICT,
    identity_version text NOT NULL CHECK (
        identity_version = 'answer-identity.g3.5.v1'
    ),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (
        answer_candidate_id,
        case_id,
        question_revision_id,
        frame_revision_id,
        plan_revision_id,
        plan_adoption_id,
        version_number
    ),
    FOREIGN KEY (
        plan_adoption_id,
        plan_revision_id,
        question_revision_id,
        frame_revision_id,
        plan_adoption_content_sha256
    ) REFERENCES waje_vnext.plan_adoption_records (
        plan_adoption_id,
        plan_revision_id,
        question_revision_id,
        frame_revision_id,
        content_sha256
    ) ON DELETE RESTRICT,
    CHECK (
        (version_number = 1 AND prior_answer_version_id IS NULL)
        OR (
            version_number > 1
            AND prior_answer_version_id IS NOT NULL
        )
    )
);

CREATE TABLE waje_vnext.evidence_use_bindings (
    evidence_use_binding_id char(64) PRIMARY KEY
        CHECK (evidence_use_binding_id ~ '^[0-9a-f]{64}$'),
    evidence_record_id char(64) NOT NULL,
    evidence_record_content_sha256 char(64) NOT NULL
        CHECK (
            evidence_record_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    evidence_admission_id char(64) NOT NULL,
    evidence_admission_content_sha256 char(64) NOT NULL
        CHECK (
            evidence_admission_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    evidence_validity_id char(64) NOT NULL,
    evidence_validity_content_sha256 char(64) NOT NULL
        CHECK (
            evidence_validity_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
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
        REFERENCES waje_vnext.plan_adoption_records(plan_revision_id)
        ON DELETE RESTRICT,
    estimand_id text NOT NULL,
    evidence_requirement_id text NOT NULL,
    obligation_id text NOT NULL
        REFERENCES waje_vnext.resolved_evidence_obligations(obligation_id)
        ON DELETE RESTRICT,
    resolution_outcome_id text NOT NULL
        REFERENCES waje_vnext.measurement_resolution_outcomes(
            resolution_outcome_id
        ) ON DELETE RESTRICT,
    answer_candidate_id char(64) NOT NULL
        REFERENCES waje_vnext.provisional_answer_candidates(
            answer_candidate_id
        ) ON DELETE RESTRICT,
    proposal_claim_key text NOT NULL,
    scope_relation text NOT NULL CHECK (
        scope_relation IN (
            'exact',
            'subset',
            'superset',
            'lawful_projection',
            'lawful_aggregation',
            'disjoint',
            'unknown'
        )
    ),
    requested_claim_strength text NOT NULL CHECK (
        requested_claim_strength IN (
            'boundary_only',
            'descriptive',
            'accounting',
            'associational',
            'causal'
        )
    ),
    effective_claim_strength text NOT NULL CHECK (
        effective_claim_strength IN (
            'boundary_only',
            'descriptive',
            'accounting',
            'associational',
            'causal'
        )
    ),
    policy_version text NOT NULL CHECK (
        policy_version = 'evidence-use.g3.5.v1'
    ),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    bound_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (
        answer_candidate_id,
        proposal_claim_key,
        evidence_record_id
    ),
    UNIQUE (
        evidence_use_binding_id,
        answer_candidate_id,
        proposal_claim_key,
        content_sha256
    ),
    FOREIGN KEY (
        evidence_record_id,
        evidence_admission_id,
        evidence_admission_content_sha256
    ) REFERENCES waje_vnext.evidence_admission_records (
        evidence_record_id,
        evidence_admission_id,
        content_sha256
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        evidence_validity_id,
        evidence_record_id,
        evidence_validity_content_sha256
    ) REFERENCES waje_vnext.evidence_validity_records (
        evidence_validity_id,
        evidence_record_id,
        content_sha256
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        evidence_record_id,
        evidence_record_content_sha256
    ) REFERENCES waje_vnext.evidence_records (
        evidence_record_id,
        content_sha256
    ) ON DELETE RESTRICT,
    CHECK (
        scope_relation IN (
            'exact',
            'superset',
            'lawful_projection',
            'lawful_aggregation'
        )
    )
);

CREATE TABLE waje_vnext.obligation_satisfaction_records (
    obligation_satisfaction_id char(64) PRIMARY KEY
        CHECK (
            obligation_satisfaction_id ~ '^[0-9a-f]{64}$'
        ),
    obligation_id text NOT NULL
        REFERENCES waje_vnext.resolved_evidence_obligations(obligation_id)
        ON DELETE RESTRICT,
    obligation_content_sha256 char(64) NOT NULL
        CHECK (obligation_content_sha256 ~ '^[0-9a-f]{64}$'),
    revision_number bigint NOT NULL CHECK (revision_number >= 1),
    prior_obligation_satisfaction_id char(64),
    prior_obligation_satisfaction_content_sha256 char(64),
    satisfaction_status text NOT NULL CHECK (
        satisfaction_status IN (
            'open',
            'satisfied',
            'boundary',
            'blocked',
            'superseded'
        )
    ),
    boundary_resolution_outcome_id text
        REFERENCES waje_vnext.measurement_resolution_outcomes(
            resolution_outcome_id
        ) ON DELETE RESTRICT,
    input_set_sha256 char(64) NOT NULL
        CHECK (input_set_sha256 ~ '^[0-9a-f]{64}$'),
    reason_code text NOT NULL,
    policy_version text NOT NULL CHECK (
        policy_version = 'obligation-satisfaction.g3.5.v1'
    ),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (
        jsonb_typeof(payload) = 'object'
        AND NOT (payload ? 'evidence_use_binding_ids')
        AND NOT (payload ? 'evidence_use_binding_content_sha256s')
    ),
    recorded_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (obligation_id, revision_number),
    UNIQUE (obligation_id, input_set_sha256),
    UNIQUE (
        obligation_satisfaction_id,
        obligation_id,
        content_sha256
    ),
    FOREIGN KEY (
        prior_obligation_satisfaction_id,
        obligation_id,
        prior_obligation_satisfaction_content_sha256
    ) REFERENCES waje_vnext.obligation_satisfaction_records (
        obligation_satisfaction_id,
        obligation_id,
        content_sha256
    ) ON DELETE RESTRICT,
    CHECK (
        (
            revision_number = 1
            AND prior_obligation_satisfaction_id IS NULL
            AND prior_obligation_satisfaction_content_sha256 IS NULL
        )
        OR (
            revision_number > 1
            AND prior_obligation_satisfaction_id IS NOT NULL
            AND prior_obligation_satisfaction_content_sha256 IS NOT NULL
        )
    ),
    CHECK (
        (satisfaction_status = 'boundary')
        = (boundary_resolution_outcome_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX obligation_satisfaction_one_successor
    ON waje_vnext.obligation_satisfaction_records(
        prior_obligation_satisfaction_id
    )
    WHERE prior_obligation_satisfaction_id IS NOT NULL;

CREATE TABLE waje_vnext.claim_precheck_records (
    claim_precheck_id char(64) PRIMARY KEY
        CHECK (claim_precheck_id ~ '^[0-9a-f]{64}$'),
    answer_candidate_id char(64) NOT NULL
        REFERENCES waje_vnext.provisional_answer_candidates(
            answer_candidate_id
        ) ON DELETE RESTRICT,
    proposal_claim_key text NOT NULL,
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
        REFERENCES waje_vnext.plan_adoption_records(plan_revision_id)
        ON DELETE RESTRICT,
    plan_adoption_id char(64) NOT NULL
        REFERENCES waje_vnext.plan_adoption_records(plan_adoption_id)
        ON DELETE RESTRICT,
    authority_snapshot_content_sha256 char(64) NOT NULL
        CHECK (
            authority_snapshot_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    target_estimand_id text NOT NULL,
    requested_strength text NOT NULL CHECK (
        requested_strength IN (
            'boundary_only',
            'descriptive',
            'accounting',
            'associational',
            'causal'
        )
    ),
    effective_strength text NOT NULL CHECK (
        effective_strength IN (
            'boundary_only',
            'descriptive',
            'accounting',
            'associational',
            'causal'
        )
    ),
    precheck_status text NOT NULL CHECK (
        precheck_status IN (
            'admissible_supported',
            'admissible_bounded',
            'admissible_boundary',
            'rejected'
        )
    ),
    policy_version text NOT NULL CHECK (
        policy_version = 'claim-precheck.g3.5.v1'
    ),
    derived_input_sha256 char(64) NOT NULL
        CHECK (derived_input_sha256 ~ '^[0-9a-f]{64}$'),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    checked_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (answer_candidate_id, proposal_claim_key),
    UNIQUE (
        claim_precheck_id,
        answer_candidate_id,
        proposal_claim_key,
        content_sha256
    )
);

CREATE TABLE waje_vnext.answer_versions (
    answer_version_id text PRIMARY KEY
        CHECK (answer_version_id ~ '^[0-9a-f]{64}$'),
    answer_candidate_id char(64) NOT NULL UNIQUE,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id)
        ON DELETE RESTRICT,
    question_revision_id text NOT NULL
        REFERENCES waje_vnext.question_revisions(question_revision_id)
        ON DELETE RESTRICT,
    frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    plan_revision_id text NOT NULL,
    plan_adoption_id char(64) NOT NULL,
    accepted_head_version bigint NOT NULL CHECK (
        accepted_head_version >= 0
    ),
    version_number bigint NOT NULL CHECK (version_number >= 1),
    prior_answer_version_id text
        REFERENCES waje_vnext.answer_versions(answer_version_id)
        ON DELETE RESTRICT,
    status text NOT NULL CHECK (status = 'provisional'),
    identity_version text NOT NULL CHECK (
        identity_version = 'answer-identity.g3.5.v1'
    ),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (case_id, version_number),
    UNIQUE (
        answer_version_id,
        answer_candidate_id,
        content_sha256
    ),
    UNIQUE (answer_version_id, answer_candidate_id),
    UNIQUE (answer_version_id, content_sha256),
    FOREIGN KEY (
        answer_candidate_id,
        case_id,
        question_revision_id,
        frame_revision_id,
        plan_revision_id,
        plan_adoption_id,
        version_number
    ) REFERENCES waje_vnext.provisional_answer_candidates (
        answer_candidate_id,
        case_id,
        question_revision_id,
        frame_revision_id,
        plan_revision_id,
        plan_adoption_id,
        version_number
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        plan_adoption_id,
        plan_revision_id,
        question_revision_id,
        frame_revision_id
    ) REFERENCES waje_vnext.plan_adoption_records (
        plan_adoption_id,
        plan_revision_id,
        question_revision_id,
        frame_revision_id
    ) ON DELETE RESTRICT,
    CHECK (
        payload->>'status' = 'provisional'
        AND COALESCE(payload->>'publication_state', 'provisional')
            <> 'settled'
    ),
    CHECK (
        (version_number = 1 AND prior_answer_version_id IS NULL)
        OR (
            version_number > 1
            AND prior_answer_version_id IS NOT NULL
        )
    )
);

ALTER TABLE waje_vnext.provisional_answer_candidates
    ADD CONSTRAINT answer_candidate_prior_answer_fk
    FOREIGN KEY (prior_answer_version_id)
    REFERENCES waje_vnext.answer_versions(answer_version_id)
    ON DELETE RESTRICT;

CREATE TABLE waje_vnext.answer_claim_records (
    claim_id char(64) PRIMARY KEY
        CHECK (claim_id ~ '^[0-9a-f]{64}$'),
    answer_version_id text NOT NULL
        REFERENCES waje_vnext.answer_versions(answer_version_id)
        ON DELETE RESTRICT,
    answer_candidate_id char(64) NOT NULL,
    proposal_claim_key text NOT NULL,
    claim_ordinal bigint NOT NULL CHECK (claim_ordinal > 0),
    claim_precheck_id char(64) NOT NULL UNIQUE,
    claim_precheck_content_sha256 char(64) NOT NULL
        CHECK (
            claim_precheck_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    target_estimand_id text NOT NULL,
    authority_path text NOT NULL CHECK (
        authority_path IN ('evidence_use', 'boundary')
    ),
    claim_strength text NOT NULL CHECK (
        claim_strength IN (
            'boundary_only',
            'descriptive',
            'accounting',
            'associational',
            'causal'
        )
    ),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (answer_version_id, claim_ordinal),
    UNIQUE (answer_candidate_id, proposal_claim_key),
    UNIQUE (
        claim_id,
        answer_version_id,
        answer_candidate_id,
        content_sha256
    ),
    FOREIGN KEY (
        answer_version_id,
        answer_candidate_id
    ) REFERENCES waje_vnext.answer_versions (
        answer_version_id,
        answer_candidate_id
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        claim_precheck_id,
        answer_candidate_id,
        proposal_claim_key,
        claim_precheck_content_sha256
    ) REFERENCES waje_vnext.claim_precheck_records (
        claim_precheck_id,
        answer_candidate_id,
        proposal_claim_key,
        content_sha256
    ) ON DELETE RESTRICT,
    CHECK (
        authority_path <> 'boundary'
        OR claim_strength = 'boundary_only'
    )
);

CREATE TABLE waje_vnext.answer_claim_evidence_use_bindings (
    claim_id char(64) NOT NULL
        REFERENCES waje_vnext.answer_claim_records(claim_id)
        ON DELETE RESTRICT,
    evidence_use_binding_id char(64) NOT NULL
        REFERENCES waje_vnext.evidence_use_bindings(
            evidence_use_binding_id
        ) ON DELETE RESTRICT,
    binding_ordinal bigint NOT NULL CHECK (binding_ordinal > 0),
    PRIMARY KEY (claim_id, evidence_use_binding_id),
    UNIQUE (claim_id, binding_ordinal)
);

CREATE TABLE waje_vnext.answer_claim_boundary_satisfactions (
    claim_id char(64) NOT NULL
        REFERENCES waje_vnext.answer_claim_records(claim_id)
        ON DELETE RESTRICT,
    obligation_satisfaction_id char(64) NOT NULL
        REFERENCES waje_vnext.obligation_satisfaction_records(
            obligation_satisfaction_id
        ) ON DELETE RESTRICT,
    binding_ordinal bigint NOT NULL CHECK (binding_ordinal > 0),
    PRIMARY KEY (claim_id, obligation_satisfaction_id),
    UNIQUE (claim_id, binding_ordinal)
);

CREATE TABLE waje_vnext.settlement_precondition_reports (
    settlement_precondition_report_id char(64) PRIMARY KEY
        CHECK (
            settlement_precondition_report_id ~ '^[0-9a-f]{64}$'
        ),
    profile text NOT NULL CHECK (
        profile IN ('conformance', 'production')
    ),
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id)
        ON DELETE RESTRICT,
    question_revision_id text NOT NULL
        REFERENCES waje_vnext.question_revisions(question_revision_id)
        ON DELETE RESTRICT,
    frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    plan_revision_id text NOT NULL,
    plan_adoption_id char(64) NOT NULL,
    plan_adoption_content_sha256 char(64) NOT NULL
        CHECK (plan_adoption_content_sha256 ~ '^[0-9a-f]{64}$'),
    accepted_head_version bigint NOT NULL CHECK (
        accepted_head_version >= 0
    ),
    answer_version_id text NOT NULL,
    answer_version_content_sha256 char(64) NOT NULL
        CHECK (answer_version_content_sha256 ~ '^[0-9a-f]{64}$'),
    trace_manifest_id text NOT NULL
        REFERENCES waje_vnext.run_trace_manifests(trace_manifest_id)
        ON DELETE RESTRICT,
    trace_manifest_content_sha256 char(64) NOT NULL
        CHECK (
            trace_manifest_content_sha256 ~ '^[0-9a-f]{64}$'
        ),
    precondition_status text NOT NULL CHECK (
        precondition_status IN (
            'eligible_for_future_settlement',
            'blocked'
        )
    ),
    derived_input_sha256 char(64) NOT NULL
        CHECK (derived_input_sha256 ~ '^[0-9a-f]{64}$'),
    policy_version text NOT NULL CHECK (
        policy_version = 'settlement-precondition.g3.5.v1'
    ),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (
        answer_version_id,
        derived_input_sha256
    ),
    FOREIGN KEY (
        plan_adoption_id,
        plan_revision_id,
        question_revision_id,
        frame_revision_id,
        plan_adoption_content_sha256
    ) REFERENCES waje_vnext.plan_adoption_records (
        plan_adoption_id,
        plan_revision_id,
        question_revision_id,
        frame_revision_id,
        content_sha256
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        answer_version_id,
        answer_version_content_sha256
    ) REFERENCES waje_vnext.answer_versions (
        answer_version_id,
        content_sha256
    ) ON DELETE RESTRICT,
    CHECK (
        profile <> 'conformance'
        OR precondition_status = 'blocked'
    ),
    CHECK (
        precondition_status = 'blocked'
        OR jsonb_array_length(
            COALESCE(payload->'fail_reason_codes', '[]'::jsonb)
        ) = 0
    )
);

CREATE TABLE waje_vnext.reviewer_objections (
    objection_id text PRIMARY KEY,
    objection_key text NOT NULL,
    revision_number bigint NOT NULL CHECK (revision_number >= 1),
    prior_objection_id text
        REFERENCES waje_vnext.reviewer_objections(objection_id)
        ON DELETE RESTRICT,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id)
        ON DELETE RESTRICT,
    answer_version_id text NOT NULL
        REFERENCES waje_vnext.answer_versions(answer_version_id)
        ON DELETE RESTRICT,
    claim_id char(64) NOT NULL
        REFERENCES waje_vnext.answer_claim_records(claim_id)
        ON DELETE RESTRICT,
    severity text NOT NULL CHECK (
        severity IN ('advisory', 'blocking')
    ),
    status text NOT NULL CHECK (
        status IN ('open', 'resolved', 'accepted_limitation')
    ),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (case_id, objection_key, revision_number),
    CHECK (
        (revision_number = 1 AND prior_objection_id IS NULL)
        OR (revision_number > 1 AND prior_objection_id IS NOT NULL)
    )
);

CREATE TABLE waje_vnext.workflow_projection_snapshots (
    snapshot_id char(64) PRIMARY KEY
        CHECK (snapshot_id ~ '^[0-9a-f]{64}$'),
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id)
        ON DELETE RESTRICT,
    applied_cursor bigint NOT NULL CHECK (applied_cursor >= 0),
    realm text NOT NULL CHECK (
        realm IN ('conformance', 'production')
    ),
    evidence_profile text NOT NULL CHECK (
        evidence_profile IN ('conformance', 'production')
    ),
    accepted_question_revision_id text,
    accepted_question_content_sha256 char(64),
    accepted_frame_revision_id text,
    accepted_frame_content_sha256 char(64),
    accepted_plan_revision_id text,
    accepted_plan_content_sha256 char(64),
    accepted_plan_adoption_id char(64),
    accepted_plan_adoption_sha256 char(64),
    publication_state text NOT NULL CHECK (
        publication_state IN (
            'not_ready',
            'provisional',
            'blocked'
        )
    ),
    accepted_answer_version_id text
        REFERENCES waje_vnext.answer_versions(answer_version_id)
        ON DELETE RESTRICT,
    delivery_state text NOT NULL CHECK (
        delivery_state IN ('not_delivered', 'superseded')
    ),
    projection_policy_version text NOT NULL CHECK (
        projection_policy_version = 'workflow-projection.g3.5.v1'
    ),
    projection_policy_sha256 char(64) NOT NULL
        CHECK (
            projection_policy_sha256 ~ '^[0-9a-f]{64}$'
        ),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (case_id, applied_cursor),
    UNIQUE (snapshot_id, case_id, applied_cursor, content_sha256),
    FOREIGN KEY (
        accepted_question_revision_id,
        accepted_question_content_sha256
    ) REFERENCES waje_vnext.question_revisions (
        question_revision_id,
        content_sha256
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        accepted_frame_revision_id,
        accepted_frame_content_sha256
    ) REFERENCES waje_vnext.analysis_frame_revisions (
        frame_revision_id,
        content_sha256
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        accepted_plan_adoption_id,
        accepted_plan_revision_id,
        accepted_plan_adoption_sha256
    ) REFERENCES waje_vnext.plan_adoption_records (
        plan_adoption_id,
        plan_revision_id,
        content_sha256
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        accepted_plan_revision_id,
        accepted_plan_content_sha256
    ) REFERENCES waje_vnext.work_plan_revisions (
        plan_revision_id,
        content_sha256
    ) ON DELETE RESTRICT,
    CHECK (realm = evidence_profile),
    CHECK (
        (
            accepted_question_revision_id IS NULL
            AND accepted_question_content_sha256 IS NULL
            AND accepted_frame_revision_id IS NULL
            AND accepted_frame_content_sha256 IS NULL
            AND accepted_plan_revision_id IS NULL
            AND accepted_plan_content_sha256 IS NULL
            AND accepted_plan_adoption_id IS NULL
            AND accepted_plan_adoption_sha256 IS NULL
        )
        OR (
            accepted_question_revision_id IS NOT NULL
            AND accepted_question_content_sha256 IS NOT NULL
            AND accepted_frame_revision_id IS NOT NULL
            AND accepted_frame_content_sha256 IS NOT NULL
            AND accepted_plan_revision_id IS NOT NULL
            AND accepted_plan_content_sha256 IS NOT NULL
            AND accepted_plan_adoption_id IS NOT NULL
            AND accepted_plan_adoption_sha256 IS NOT NULL
        )
    ),
    CHECK (
        (
            publication_state = 'provisional'
            AND accepted_answer_version_id IS NOT NULL
        )
        OR (
            publication_state <> 'provisional'
            AND accepted_answer_version_id IS NULL
        )
    ),
    CHECK (
        COALESCE(payload->>'publication_state', publication_state)
            <> 'settled'
        AND COALESCE(payload->>'delivery_state', delivery_state)
            <> 'delivered'
        AND COALESCE(payload->>'execution_state', '') <> 'completed'
    )
);

CREATE TABLE waje_vnext.workflow_application_receipts (
    receipt_id char(64) PRIMARY KEY
        CHECK (receipt_id ~ '^[0-9a-f]{64}$'),
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id)
        ON DELETE RESTRICT,
    cursor bigint NOT NULL CHECK (cursor >= 1),
    source_event_id text NOT NULL,
    source_event_sha256 char(64) NOT NULL
        CHECK (source_event_sha256 ~ '^[0-9a-f]{64}$'),
    fact_id char(64) NOT NULL
        CHECK (fact_id ~ '^[0-9a-f]{64}$'),
    fact_sha256 char(64) NOT NULL
        CHECK (fact_sha256 ~ '^[0-9a-f]{64}$'),
    prior_receipt_id char(64),
    prior_snapshot_sha256 char(64) NOT NULL
        CHECK (prior_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    resulting_snapshot_id char(64) NOT NULL,
    resulting_snapshot_sha256 char(64) NOT NULL
        CHECK (resulting_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    content_sha256 char(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    applied_at timestamptz NOT NULL,
    schema_epoch bigint NOT NULL CHECK (schema_epoch = 3),
    UNIQUE (case_id, cursor),
    UNIQUE (case_id, source_event_id),
    UNIQUE (case_id, source_event_sha256),
    UNIQUE (case_id, fact_id),
    UNIQUE (prior_receipt_id),
    UNIQUE (receipt_id, case_id, cursor, content_sha256),
    UNIQUE (receipt_id, case_id, cursor),
    FOREIGN KEY (
        case_id,
        cursor,
        source_event_id
    ) REFERENCES waje_vnext.event_journal (
        case_id,
        cursor,
        event_id
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        resulting_snapshot_id,
        case_id,
        cursor,
        resulting_snapshot_sha256
    ) REFERENCES waje_vnext.workflow_projection_snapshots (
        snapshot_id,
        case_id,
        applied_cursor,
        content_sha256
    ) ON DELETE RESTRICT,
    FOREIGN KEY (prior_receipt_id)
        REFERENCES waje_vnext.workflow_application_receipts(receipt_id)
        ON DELETE RESTRICT,
    CHECK (
        (cursor = 1 AND prior_receipt_id IS NULL)
        OR (cursor > 1 AND prior_receipt_id IS NOT NULL)
    )
);

CREATE TABLE waje_vnext.workflow_projection_heads (
    case_id text PRIMARY KEY
        REFERENCES waje_vnext.investigation_cases(case_id)
        ON DELETE RESTRICT,
    version bigint NOT NULL CHECK (version >= 0),
    last_applied_cursor bigint NOT NULL CHECK (
        last_applied_cursor >= 0
    ),
    snapshot_id char(64) NOT NULL,
    snapshot_sha256 char(64) NOT NULL
        CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    last_receipt_id char(64),
    realm text NOT NULL CHECK (
        realm IN ('conformance', 'production')
    ),
    evidence_profile text NOT NULL CHECK (
        evidence_profile IN ('conformance', 'production')
    ),
    updated_at timestamptz NOT NULL,
    FOREIGN KEY (
        snapshot_id,
        case_id,
        last_applied_cursor,
        snapshot_sha256
    ) REFERENCES waje_vnext.workflow_projection_snapshots (
        snapshot_id,
        case_id,
        applied_cursor,
        content_sha256
    ) ON DELETE RESTRICT,
    FOREIGN KEY (
        last_receipt_id,
        case_id,
        last_applied_cursor
    ) REFERENCES waje_vnext.workflow_application_receipts (
        receipt_id,
        case_id,
        cursor
    ) ON DELETE RESTRICT,
    CHECK (version = last_applied_cursor),
    CHECK (realm = evidence_profile),
    CHECK (
        (last_applied_cursor = 0 AND last_receipt_id IS NULL)
        OR (
            last_applied_cursor > 0
            AND last_receipt_id IS NOT NULL
        )
    )
);

ALTER TABLE waje_vnext.investigation_cases
    ADD CONSTRAINT investigation_case_answer_head_fk
    FOREIGN KEY (accepted_answer_version_id)
    REFERENCES waje_vnext.answer_versions(answer_version_id)
    ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION
    waje_vnext.guard_workflow_projection_head_cas()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    prior_cursor bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'workflow_projection_heads cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.case_id <> OLD.case_id
       OR NEW.version <> OLD.version + 1
       OR NEW.last_applied_cursor <> OLD.last_applied_cursor + 1
       OR NEW.last_receipt_id IS NULL
    THEN
        RAISE EXCEPTION
            'workflow projection head update failed monotonic CAS'
            USING ERRCODE = '40001';
    END IF;
    SELECT cursor
    INTO prior_cursor
    FROM waje_vnext.workflow_application_receipts
    WHERE receipt_id = NEW.last_receipt_id;
    IF prior_cursor IS DISTINCT FROM NEW.last_applied_cursor THEN
        RAISE EXCEPTION
            'workflow projection head receipt is not current'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER workflow_projection_head_cas
    BEFORE UPDATE OR DELETE
    ON waje_vnext.workflow_projection_heads
    FOR EACH ROW EXECUTE FUNCTION
        waje_vnext.guard_workflow_projection_head_cas();

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'evidence_records',
        'capability_result_envelopes',
        'capability_result_receipts',
        'evidence_admission_records',
        'evidence_validity_records',
        'provisional_answer_candidates',
        'evidence_use_bindings',
        'obligation_satisfaction_records',
        'claim_precheck_records',
        'answer_versions',
        'answer_claim_records',
        'answer_claim_evidence_use_bindings',
        'answer_claim_boundary_satisfactions',
        'settlement_precondition_reports',
        'reviewer_objections',
        'workflow_projection_snapshots',
        'workflow_application_receipts'
    ]
    LOOP
        EXECUTE format(
            'CREATE TRIGGER %I_immutable
             BEFORE UPDATE OR DELETE ON waje_vnext.%I
             FOR EACH ROW EXECUTE FUNCTION
             waje_vnext.reject_immutable_change()',
            table_name,
            table_name
        );
    END LOOP;
END;
$$;

CREATE INDEX evidence_admission_obligation_idx
    ON waje_vnext.evidence_admission_records(
        obligation_id,
        admission_status,
        admitted_at
    );

CREATE INDEX evidence_use_candidate_claim_idx
    ON waje_vnext.evidence_use_bindings(
        answer_candidate_id,
        proposal_claim_key
    );

CREATE INDEX answer_case_version_idx
    ON waje_vnext.answer_versions(case_id, version_number);

CREATE INDEX workflow_snapshot_case_cursor_idx
    ON waje_vnext.workflow_projection_snapshots(
        case_id,
        applied_cursor
    );
