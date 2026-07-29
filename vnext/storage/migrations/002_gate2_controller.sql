CREATE TABLE waje_vnext.action_records (
    action_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    expected_head_version bigint NOT NULL CHECK (expected_head_version >= 0),
    idempotency_key text NOT NULL,
    proposal_sha256 text NOT NULL CHECK (proposal_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    recorded_at timestamptz NOT NULL,
    UNIQUE (case_id, idempotency_key)
);

ALTER TABLE waje_vnext.action_receipts
    ADD CONSTRAINT action_receipt_action_fk
    FOREIGN KEY (action_id)
    REFERENCES waje_vnext.action_records(action_id)
    ON DELETE RESTRICT;

ALTER TABLE waje_vnext.outbox_messages
    ADD CONSTRAINT outbox_action_fk
    FOREIGN KEY (action_id)
    REFERENCES waje_vnext.action_records(action_id)
    ON DELETE RESTRICT;

CREATE TABLE waje_vnext.user_decision_requests (
    decision_request_id text PRIMARY KEY,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    action_id text NOT NULL
        REFERENCES waje_vnext.action_records(action_id) ON DELETE RESTRICT,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    requested_at timestamptz NOT NULL,
    UNIQUE (case_id, action_id)
);

CREATE TABLE waje_vnext.effect_attempts (
    effect_attempt_id text PRIMARY KEY,
    outbox_message_id text NOT NULL
        REFERENCES waje_vnext.outbox_messages(outbox_message_id)
        ON DELETE RESTRICT,
    case_id text NOT NULL
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    attempt_number bigint NOT NULL CHECK (attempt_number >= 1),
    prior_attempt_id text
        REFERENCES waje_vnext.effect_attempts(effect_attempt_id)
        ON DELETE RESTRICT,
    status text NOT NULL CHECK (
        status IN ('succeeded', 'retryable_failure', 'terminal_failure')
    ),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    started_at timestamptz NOT NULL,
    completed_at timestamptz NOT NULL,
    UNIQUE (outbox_message_id, attempt_number),
    CHECK (completed_at >= started_at),
    CHECK (
        (attempt_number = 1 AND prior_attempt_id IS NULL)
        OR (attempt_number > 1 AND prior_attempt_id IS NOT NULL)
    )
);

CREATE TABLE waje_vnext.controller_leases (
    case_id text PRIMARY KEY
        REFERENCES waje_vnext.investigation_cases(case_id) ON DELETE RESTRICT,
    run_id text NOT NULL,
    owner_id text NOT NULL,
    fencing_token bigint NOT NULL CHECK (fencing_token >= 1),
    acquired_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    CHECK (expires_at > acquired_at)
);

CREATE TRIGGER action_records_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.action_records
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER user_decision_requests_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.user_decision_requests
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
CREATE TRIGGER effect_attempts_immutable
    BEFORE UPDATE OR DELETE ON waje_vnext.effect_attempts
    FOR EACH ROW EXECUTE FUNCTION waje_vnext.reject_immutable_change();
