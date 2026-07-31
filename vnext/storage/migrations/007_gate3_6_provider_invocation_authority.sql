-- Gate 3.6 replaces the development-only model invocation persistence
-- contract. Existing runtime rows carry too little request/configuration
-- identity and cannot be interpreted under this contract.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM waje_vnext.logical_model_jobs LIMIT 1)
       OR EXISTS (
            SELECT 1 FROM waje_vnext.provider_attempt_requests LIMIT 1
       )
       OR EXISTS (
            SELECT 1 FROM waje_vnext.provider_attempt_receipts LIMIT 1
       )
       OR EXISTS (SELECT 1 FROM waje_vnext.durable_model_results LIMIT 1)
    THEN
        RAISE EXCEPTION
            'G3.6 provider invocation authority requires empty superseded model runtime tables; reset the disposable development database and reapply migrations'
            USING ERRCODE = '55000';
    END IF;
END;
$$;

ALTER TABLE waje_vnext.logical_model_jobs
    ADD COLUMN configuration_sha256 char(64) NOT NULL
        CHECK (configuration_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN model_request_artifact_sha256 char(64) NOT NULL
        CHECK (model_request_artifact_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN provider_request_sha256 char(64) NOT NULL
        CHECK (provider_request_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN output_contract_ref text NOT NULL,
    ADD CONSTRAINT logical_model_job_request_identity UNIQUE (
        logical_model_job_id,
        configuration_sha256,
        model_request_artifact_sha256,
        provider_request_sha256
    ),
    ADD CONSTRAINT logical_model_job_result_identity UNIQUE (
        logical_model_job_id,
        configuration_sha256,
        model_request_artifact_sha256,
        output_contract_ref
    );

ALTER TABLE waje_vnext.provider_attempt_requests
    ADD COLUMN provider_idempotency_key text NOT NULL UNIQUE,
    ADD COLUMN request_sha256 char(64) NOT NULL
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN model_request_artifact_sha256 char(64) NOT NULL
        CHECK (model_request_artifact_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN configuration_sha256 char(64) NOT NULL
        CHECK (configuration_sha256 ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT provider_attempt_job_request_fk FOREIGN KEY (
        logical_model_job_id,
        configuration_sha256,
        model_request_artifact_sha256,
        request_sha256
    ) REFERENCES waje_vnext.logical_model_jobs (
        logical_model_job_id,
        configuration_sha256,
        model_request_artifact_sha256,
        provider_request_sha256
    ) ON DELETE RESTRICT,
    ADD CONSTRAINT provider_attempt_job_attempt_key UNIQUE (
        logical_model_job_id,
        provider_attempt_id
    ),
    ADD CONSTRAINT provider_attempt_prior_same_job_fk FOREIGN KEY (
        logical_model_job_id,
        prior_provider_attempt_id
    ) REFERENCES waje_vnext.provider_attempt_requests (
        logical_model_job_id,
        provider_attempt_id
    ) ON DELETE RESTRICT,
    ADD CONSTRAINT provider_attempt_prior_shape CHECK (
        (attempt_number = 1 AND prior_provider_attempt_id IS NULL)
        OR (attempt_number > 1 AND prior_provider_attempt_id IS NOT NULL)
    );

CREATE OR REPLACE FUNCTION waje_vnext.verify_provider_attempt_sequence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    prior_attempt_number bigint;
BEGIN
    IF NEW.attempt_number = 1 THEN
        RETURN NEW;
    END IF;
    SELECT attempt_number INTO prior_attempt_number
    FROM waje_vnext.provider_attempt_requests
    WHERE logical_model_job_id = NEW.logical_model_job_id
      AND provider_attempt_id = NEW.prior_provider_attempt_id;
    IF NOT FOUND OR prior_attempt_number <> NEW.attempt_number - 1 THEN
        RAISE EXCEPTION
            'provider attempt prior must be the preceding same-job attempt'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER provider_attempt_sequence
    BEFORE INSERT ON waje_vnext.provider_attempt_requests
    FOR EACH ROW EXECUTE FUNCTION
        waje_vnext.verify_provider_attempt_sequence();

ALTER TABLE waje_vnext.provider_attempt_receipts
    ADD COLUMN provider_response_id text,
    ADD COLUMN output_sha256 char(64)
        CHECK (
            output_sha256 IS NULL
            OR output_sha256 ~ '^[0-9a-f]{64}$'
        ),
    ADD CONSTRAINT provider_attempt_receipt_disposition_check CHECK (
        disposition IN (
            'succeeded',
            'retryable_failure',
            'terminal_failure',
            'refusal',
            'incomplete',
            'multiple_tool_calls',
            'superseded'
        )
    ),
    ADD CONSTRAINT provider_success_has_output_check CHECK (
        (
            disposition = 'succeeded'
            AND output_sha256 IS NOT NULL
            AND provider_response_id IS NOT NULL
        )
        OR (disposition <> 'succeeded')
    ),
    ADD CONSTRAINT provider_receipt_attempt_job_fk FOREIGN KEY (
        logical_model_job_id,
        provider_attempt_id
    ) REFERENCES waje_vnext.provider_attempt_requests (
        logical_model_job_id,
        provider_attempt_id
    ) ON DELETE RESTRICT;

ALTER TABLE waje_vnext.provider_attempt_receipts
    ADD CONSTRAINT provider_success_pair_identity UNIQUE (
        provider_attempt_receipt_id,
        logical_model_job_id,
        provider_attempt_id,
        output_sha256
    );

CREATE UNIQUE INDEX provider_one_success_per_logical_job
    ON waje_vnext.provider_attempt_receipts(logical_model_job_id)
    WHERE disposition = 'succeeded';

CREATE UNIQUE INDEX provider_response_identity_unique
    ON waje_vnext.provider_attempt_receipts(
        logical_model_job_id,
        provider_response_id
    )
    WHERE provider_response_id IS NOT NULL;

ALTER TABLE waje_vnext.durable_model_results
    ADD COLUMN provider_attempt_receipt_id text NOT NULL UNIQUE,
    ADD COLUMN model_request_artifact_sha256 char(64) NOT NULL
        CHECK (model_request_artifact_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN configuration_sha256 char(64) NOT NULL
        CHECK (configuration_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN result_contract_ref text NOT NULL,
    ADD CONSTRAINT durable_result_success_receipt_fk FOREIGN KEY (
        provider_attempt_receipt_id
    ) REFERENCES waje_vnext.provider_attempt_receipts (
        provider_attempt_receipt_id
    ) ON DELETE RESTRICT,
    ADD CONSTRAINT durable_result_job_attempt_fk FOREIGN KEY (
        logical_model_job_id,
        provider_attempt_id
    ) REFERENCES waje_vnext.provider_attempt_requests (
        logical_model_job_id,
        provider_attempt_id
    ) ON DELETE RESTRICT,
    ADD CONSTRAINT durable_result_job_contract_fk FOREIGN KEY (
        logical_model_job_id,
        configuration_sha256,
        model_request_artifact_sha256,
        result_contract_ref
    ) REFERENCES waje_vnext.logical_model_jobs (
        logical_model_job_id,
        configuration_sha256,
        model_request_artifact_sha256,
        output_contract_ref
    ) ON DELETE RESTRICT,
    ADD CONSTRAINT durable_result_success_identity_fk FOREIGN KEY (
        provider_attempt_receipt_id,
        logical_model_job_id,
        provider_attempt_id,
        output_sha256
    ) REFERENCES waje_vnext.provider_attempt_receipts (
        provider_attempt_receipt_id,
        logical_model_job_id,
        provider_attempt_id,
        output_sha256
    ) ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION waje_vnext.verify_provider_success_pair()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    receipt_row waje_vnext.provider_attempt_receipts%ROWTYPE;
    result_row waje_vnext.durable_model_results%ROWTYPE;
BEGIN
    IF TG_TABLE_NAME = 'provider_attempt_receipts' THEN
        IF NEW.disposition <> 'succeeded' THEN
            RETURN NEW;
        END IF;
        SELECT * INTO result_row
        FROM waje_vnext.durable_model_results
        WHERE provider_attempt_receipt_id = NEW.provider_attempt_receipt_id;
        IF NOT FOUND
           OR result_row.logical_model_job_id <> NEW.logical_model_job_id
           OR result_row.provider_attempt_id <> NEW.provider_attempt_id
           OR result_row.output_sha256 <> NEW.output_sha256
        THEN
            RAISE EXCEPTION
                'successful provider receipt lacks its exact typed result'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    SELECT * INTO receipt_row
    FROM waje_vnext.provider_attempt_receipts
    WHERE provider_attempt_receipt_id = NEW.provider_attempt_receipt_id;
    IF NOT FOUND
       OR receipt_row.disposition <> 'succeeded'
       OR receipt_row.logical_model_job_id <> NEW.logical_model_job_id
       OR receipt_row.provider_attempt_id <> NEW.provider_attempt_id
       OR receipt_row.output_sha256 <> NEW.output_sha256
    THEN
        RAISE EXCEPTION
            'typed result lacks its exact successful provider receipt'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER provider_success_receipt_pair
    AFTER INSERT ON waje_vnext.provider_attempt_receipts
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION
        waje_vnext.verify_provider_success_pair();

CREATE CONSTRAINT TRIGGER provider_success_result_pair
    AFTER INSERT ON waje_vnext.durable_model_results
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION
        waje_vnext.verify_provider_success_pair();
