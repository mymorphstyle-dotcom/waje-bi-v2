CREATE SCHEMA IF NOT EXISTS waje_vnext;

CREATE TABLE waje_vnext.schema_migrations (
    version bigint PRIMARY KEY,
    name text NOT NULL,
    checksum_sha256 text NOT NULL CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE waje_vnext.investigation_cases (
    case_id text PRIMARY KEY,
    thread_id text NOT NULL,
    lifecycle text NOT NULL CHECK (
        lifecycle IN ('open', 'waiting_for_user', 'stopped', 'closed')
    ),
    head_version bigint NOT NULL DEFAULT 0 CHECK (head_version >= 0),
    accepted_frame_revision_id text,
    accepted_plan_revision_id text,
    accepted_answer_version_id text,
    opened_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (updated_at >= opened_at),
    CHECK (
        accepted_plan_revision_id IS NULL
        OR accepted_frame_revision_id IS NOT NULL
    ),
    CHECK (
        accepted_answer_version_id IS NULL
        OR accepted_plan_revision_id IS NOT NULL
    )
);

CREATE TABLE waje_vnext.event_stream_heads (
    case_id text PRIMARY KEY
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    last_cursor bigint NOT NULL DEFAULT 0 CHECK (last_cursor >= 0)
);

CREATE TABLE waje_vnext.case_mailbox_heads (
    case_id text PRIMARY KEY
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    last_sequence bigint NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
    authority_epoch bigint NOT NULL DEFAULT 0 CHECK (authority_epoch >= 0),
    updated_at timestamptz NOT NULL
);

CREATE TABLE waje_vnext.case_mailbox_messages (
    message_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    sequence bigint NOT NULL CHECK (sequence >= 1),
    authority_epoch bigint NOT NULL CHECK (authority_epoch >= 0),
    message_kind text NOT NULL CHECK (
        message_kind IN (
            'user_message',
            'user_correction',
            'user_challenge',
            'user_scope_revision'
        )
    ),
    operation_id text NOT NULL,
    idempotency_key text NOT NULL,
    causation_id text NOT NULL,
    correlation_id text NOT NULL,
    authority_revision bigint NOT NULL CHECK (authority_revision >= 0),
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    UNIQUE (case_id, sequence),
    UNIQUE (case_id, idempotency_key)
);

CREATE TABLE waje_vnext.analysis_frame_revisions (
    frame_revision_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    revision_number bigint NOT NULL CHECK (revision_number >= 1),
    prior_frame_revision_id text
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    UNIQUE (case_id, revision_number),
    CHECK (
        (revision_number = 1 AND prior_frame_revision_id IS NULL)
        OR (revision_number > 1 AND prior_frame_revision_id IS NOT NULL)
    )
);

CREATE TABLE waje_vnext.work_plan_revisions (
    plan_revision_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    revision_number bigint NOT NULL CHECK (revision_number >= 1),
    prior_plan_revision_id text
        REFERENCES waje_vnext.work_plan_revisions(plan_revision_id)
        ON DELETE RESTRICT,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    UNIQUE (case_id, revision_number),
    CHECK (
        (revision_number = 1 AND prior_plan_revision_id IS NULL)
        OR (revision_number > 1 AND prior_plan_revision_id IS NOT NULL)
    )
);

CREATE TABLE waje_vnext.evidence_records (
    evidence_record_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    plan_revision_id text NOT NULL
        REFERENCES waje_vnext.work_plan_revisions(plan_revision_id)
        ON DELETE RESTRICT,
    task_id text NOT NULL,
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL
);

CREATE TABLE waje_vnext.answer_versions (
    answer_version_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    plan_revision_id text NOT NULL
        REFERENCES waje_vnext.work_plan_revisions(plan_revision_id)
        ON DELETE RESTRICT,
    version_number bigint NOT NULL CHECK (version_number >= 1),
    prior_answer_version_id text
        REFERENCES waje_vnext.answer_versions(answer_version_id)
        ON DELETE RESTRICT,
    status text NOT NULL CHECK (status IN ('provisional', 'settled')),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    UNIQUE (case_id, version_number),
    CHECK (
        (version_number = 1 AND prior_answer_version_id IS NULL)
        OR (version_number > 1 AND prior_answer_version_id IS NOT NULL)
    )
);

ALTER TABLE waje_vnext.investigation_cases
    ADD CONSTRAINT investigation_case_frame_head_fk
    FOREIGN KEY (accepted_frame_revision_id)
    REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
    ON DELETE RESTRICT,
    ADD CONSTRAINT investigation_case_plan_head_fk
    FOREIGN KEY (accepted_plan_revision_id)
    REFERENCES waje_vnext.work_plan_revisions(plan_revision_id)
    ON DELETE RESTRICT,
    ADD CONSTRAINT investigation_case_answer_head_fk
    FOREIGN KEY (accepted_answer_version_id)
    REFERENCES waje_vnext.answer_versions(answer_version_id)
    ON DELETE RESTRICT;

CREATE TABLE waje_vnext.interpretation_records (
    interpretation_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    frame_revision_id text NOT NULL
        REFERENCES waje_vnext.analysis_frame_revisions(frame_revision_id)
        ON DELETE RESTRICT,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL
);

CREATE TABLE waje_vnext.decision_records (
    decision_record_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL
);

CREATE TABLE waje_vnext.reviewer_objections (
    objection_id text PRIMARY KEY,
    objection_key text NOT NULL,
    revision_number bigint NOT NULL CHECK (revision_number >= 1),
    prior_objection_id text
        REFERENCES waje_vnext.reviewer_objections(objection_id)
        ON DELETE RESTRICT,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    answer_version_id text NOT NULL
        REFERENCES waje_vnext.answer_versions(answer_version_id)
        ON DELETE RESTRICT,
    claim_id text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('advisory', 'blocking')),
    status text NOT NULL CHECK (
        status IN ('open', 'resolved', 'accepted_limitation')
    ),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    UNIQUE (case_id, objection_key, revision_number),
    CHECK (
        (revision_number = 1 AND prior_objection_id IS NULL)
        OR (revision_number > 1 AND prior_objection_id IS NOT NULL)
    )
);

CREATE TABLE waje_vnext.context_packets (
    packet_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    head_version bigint NOT NULL CHECK (head_version >= 0),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    built_at timestamptz NOT NULL
);

CREATE TABLE waje_vnext.action_receipts (
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    idempotency_key text NOT NULL,
    action_id text NOT NULL,
    request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    result_sha256 text NOT NULL CHECK (result_sha256 ~ '^[0-9a-f]{64}$'),
    event_cursor bigint NOT NULL CHECK (event_cursor >= 1),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (case_id, idempotency_key),
    UNIQUE (action_id)
);

CREATE TABLE waje_vnext.event_journal (
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    cursor bigint NOT NULL CHECK (cursor >= 1),
    event_id text NOT NULL UNIQUE,
    event_type text NOT NULL,
    recorded_at timestamptz NOT NULL,
    operation_id text NOT NULL,
    idempotency_key text NOT NULL,
    causation_id text NOT NULL,
    correlation_id text NOT NULL,
    authority_revision bigint NOT NULL CHECK (authority_revision >= 0),
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    action_id text,
    authority_ref text,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    customer_projection jsonb CHECK (
        customer_projection IS NULL
        OR jsonb_typeof(customer_projection) = 'object'
    ),
    PRIMARY KEY (case_id, cursor)
);

CREATE INDEX event_journal_action_idx
    ON waje_vnext.event_journal (case_id, action_id)
    WHERE action_id IS NOT NULL;

CREATE INDEX evidence_case_plan_task_idx
    ON waje_vnext.evidence_records (case_id, plan_revision_id, task_id);

CREATE INDEX context_packet_rebuild_idx
    ON waje_vnext.context_packets (
        case_id,
        head_version,
        content_sha256
    );

ALTER TABLE waje_vnext.action_receipts
    ADD CONSTRAINT action_receipt_event_fk
    FOREIGN KEY (case_id, event_cursor)
    REFERENCES waje_vnext.event_journal(case_id, cursor)
    ON DELETE RESTRICT;

CREATE TABLE waje_vnext.checkpoint_records (
    checkpoint_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    head_version bigint NOT NULL CHECK (head_version >= 0),
    event_cursor bigint NOT NULL,
    context_packet_id text NOT NULL
        REFERENCES waje_vnext.context_packets(packet_id) ON DELETE RESTRICT,
    context_sha256 text NOT NULL CHECK (context_sha256 ~ '^[0-9a-f]{64}$'),
    state_sha256 text NOT NULL CHECK (state_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    UNIQUE (case_id, event_cursor),
    FOREIGN KEY (case_id, event_cursor)
        REFERENCES waje_vnext.event_journal(case_id, cursor)
        ON DELETE RESTRICT
);

CREATE TABLE waje_vnext.outbox_messages (
    outbox_message_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    source_event_cursor bigint NOT NULL,
    action_id text,
    job_kind text NOT NULL CHECK (
        job_kind IN (
            'controller_wake',
            'primary_agent',
            'semantic_inspection',
            'data_probe',
            'capability',
            'sensitivity',
            'reviewer',
            'projection'
        )
    ),
    operation_id text NOT NULL,
    causation_id text NOT NULL,
    correlation_id text NOT NULL,
    authority_revision bigint NOT NULL CHECK (authority_revision >= 0),
    expected_head_version bigint NOT NULL CHECK (expected_head_version >= 0),
    expected_authority_epoch bigint NOT NULL CHECK (
        expected_authority_epoch >= 1
    ),
    idempotency_key text NOT NULL,
    destination text NOT NULL,
    contract_ref text NOT NULL,
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL,
    CHECK (
        payload ? 'operation'
        AND payload->'operation'->>'payload_sha256' = payload_sha256
        AND payload->>'payload_sha256' = payload_sha256
    ),
    UNIQUE (case_id, idempotency_key),
    FOREIGN KEY (case_id, source_event_cursor)
        REFERENCES waje_vnext.event_journal(case_id, cursor)
        ON DELETE RESTRICT
);

CREATE FUNCTION waje_vnext.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER analysis_frame_revisions_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.analysis_frame_revisions
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER work_plan_revisions_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.work_plan_revisions
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER evidence_records_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.evidence_records
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER answer_versions_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.answer_versions
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER interpretation_records_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.interpretation_records
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER decision_records_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.decision_records
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER reviewer_objections_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.reviewer_objections
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER context_packets_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.context_packets
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER action_receipts_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.action_receipts
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER checkpoint_records_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.checkpoint_records
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER outbox_messages_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.outbox_messages
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER case_mailbox_messages_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.case_mailbox_messages
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER event_journal_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.event_journal
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
