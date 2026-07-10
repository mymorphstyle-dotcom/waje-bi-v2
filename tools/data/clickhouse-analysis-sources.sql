-- BEGIN MARKET_DASHBOARD
CREATE TABLE IF NOT EXISTS __OVERALL_TABLE__
(
    snapshot_id String,
    load_revision String,
    business_date Date,
    game String,
    active_users Nullable(Decimal(38, 0)),
    new_users Nullable(Decimal(38, 0)),
    revenue Nullable(Decimal(38, 12)),
    active_user_arpu Nullable(Decimal(38, 18)),
    registrations Nullable(Decimal(38, 0)),
    new_devices Nullable(Decimal(38, 0)),
    login_accounts Nullable(Decimal(38, 0)),
    registration_rate Nullable(Decimal(38, 18)),
    zero_round_user_share Nullable(Decimal(38, 18)),
    gameplay_users Nullable(Decimal(38, 0)),
    gameplay_rounds Nullable(Decimal(38, 0)),
    app_avg_online_time Nullable(Decimal(38, 18)),
    effective_user_app_avg_online_time Nullable(Decimal(38, 18)),
    historical_paid_active_users Nullable(Decimal(38, 0)),
    first_paid_amount Nullable(Decimal(38, 12)),
    new_paid_amount Nullable(Decimal(38, 12)),
    login_user_avg_recharge Nullable(Decimal(38, 18)),
    avg_first_paid_amount Nullable(Decimal(38, 18)),
    first_paid_rate Nullable(Decimal(38, 18)),
    first_paid_users Nullable(Decimal(38, 0)),
    new_paid_rate Nullable(Decimal(38, 18)),
    new_paid_users Nullable(Decimal(38, 0)),
    paid_users Nullable(Decimal(38, 0)),
    paid_amount Nullable(Decimal(38, 12)),
    recharge_channel_fee Nullable(Decimal(38, 12)),
    withdraw_request_amount Nullable(Decimal(38, 12)),
    withdraw_arrived_users Nullable(Decimal(38, 0)),
    withdraw_arrived_amount Nullable(Decimal(38, 12)),
    withdraw_to_recharge_ratio Nullable(Decimal(38, 18)),
    withdraw_request_users Nullable(Decimal(38, 0)),
    withdraw_fee Nullable(Decimal(38, 12)),
    withdraw_user_fee Nullable(Decimal(38, 12)),
    aggregate_marketing_cost Nullable(Decimal(38, 12)),
    profit Nullable(Decimal(38, 12))
)
ENGINE = MergeTree
ORDER BY (snapshot_id, load_revision, business_date, game);

CREATE TABLE IF NOT EXISTS __CHANNEL_TABLE__
(
    snapshot_id String,
    load_revision String,
    business_date Date,
    game String,
    channel String,
    active_users Nullable(Decimal(38, 0)),
    new_users Nullable(Decimal(38, 0)),
    revenue Nullable(Decimal(38, 12)),
    active_user_arpu Nullable(Decimal(38, 18)),
    registrations Nullable(Decimal(38, 0)),
    new_devices Nullable(Decimal(38, 0)),
    login_accounts Nullable(Decimal(38, 0)),
    registration_rate Nullable(Decimal(38, 18)),
    zero_round_user_share Nullable(Decimal(38, 18)),
    gameplay_users Nullable(Decimal(38, 0)),
    gameplay_rounds Nullable(Decimal(38, 0)),
    app_avg_online_time Nullable(Decimal(38, 18)),
    effective_user_app_avg_online_time Nullable(Decimal(38, 18)),
    historical_paid_active_users Nullable(Decimal(38, 0)),
    first_paid_amount Nullable(Decimal(38, 12)),
    new_paid_amount Nullable(Decimal(38, 12)),
    login_user_avg_recharge Nullable(Decimal(38, 18)),
    avg_first_paid_amount Nullable(Decimal(38, 18)),
    first_paid_rate Nullable(Decimal(38, 18)),
    first_paid_users Nullable(Decimal(38, 0)),
    new_paid_rate Nullable(Decimal(38, 18)),
    new_paid_users Nullable(Decimal(38, 0)),
    paid_users Nullable(Decimal(38, 0)),
    paid_amount Nullable(Decimal(38, 12)),
    recharge_channel_fee Nullable(Decimal(38, 12)),
    withdraw_request_amount Nullable(Decimal(38, 12)),
    withdraw_arrived_users Nullable(Decimal(38, 0)),
    withdraw_arrived_amount Nullable(Decimal(38, 12)),
    withdraw_to_recharge_ratio Nullable(Decimal(38, 18)),
    withdraw_request_users Nullable(Decimal(38, 0)),
    withdraw_fee Nullable(Decimal(38, 12)),
    withdraw_user_fee Nullable(Decimal(38, 12)),
    aggregate_marketing_cost Nullable(Decimal(38, 12)),
    profit Nullable(Decimal(38, 12))
)
ENGINE = MergeTree
ORDER BY (snapshot_id, load_revision, business_date, game, channel);
-- END MARKET_DASHBOARD

-- BEGIN GAMEPLAY_EVENTS
CREATE TABLE IF NOT EXISTS __GAMEPLAY_TABLE__
(
    snapshot_id String,
    load_revision String,
    business_date Date,
    service_scope String,
    gameplay String,
    gameplay_users Decimal(38, 0),
    gameplay_penetration_rate Nullable(Decimal(38, 18)),
    gameplay_rounds Nullable(Decimal(38, 0)),
    rounds_per_user Nullable(Decimal(38, 18)),
    player_match_rate Nullable(Decimal(38, 18)),
    total_rounds Nullable(Decimal(38, 0)),
    service_fee_rake Nullable(Decimal(38, 12)),
    robot_cash_won Nullable(Decimal(38, 12)),
    system_rake_rate Nullable(Decimal(38, 18)),
    gameplay_profit Nullable(Decimal(38, 12)),
    profit_share Nullable(Decimal(38, 18)),
    bet_amount_share Nullable(Decimal(38, 18)),
    player_bet_amount Nullable(Decimal(38, 12)),
    player_bet_count Nullable(Decimal(38, 0)),
    betting_users_derived Nullable(Decimal(38, 0)),
    player_bet_count_per_user Nullable(Decimal(38, 18)),
    player_avg_bet_amount Nullable(Decimal(38, 18)),
    player_bet_amount_per_user Nullable(Decimal(38, 18)),
    robot_cash_lost_raw Nullable(Decimal(38, 12))
)
ENGINE = MergeTree
ORDER BY (snapshot_id, load_revision, business_date, service_scope, gameplay);

CREATE TABLE IF NOT EXISTS __GAMEPLAY_CHANNEL_TABLE__
(
    snapshot_id String,
    load_revision String,
    business_date Date,
    channel String,
    service_scope String,
    gameplay String,
    gameplay_users Decimal(38, 0),
    gameplay_penetration_rate Nullable(Decimal(38, 18)),
    gameplay_rounds Nullable(Decimal(38, 0)),
    rounds_per_user Nullable(Decimal(38, 18)),
    player_match_rate Nullable(Decimal(38, 18)),
    total_rounds Nullable(Decimal(38, 0)),
    service_fee_rake Nullable(Decimal(38, 12)),
    robot_cash_won Nullable(Decimal(38, 12)),
    system_rake_rate Nullable(Decimal(38, 18)),
    gameplay_profit Nullable(Decimal(38, 12)),
    profit_share Nullable(Decimal(38, 18)),
    bet_amount_share Nullable(Decimal(38, 18)),
    player_bet_amount Nullable(Decimal(38, 12)),
    player_bet_count Nullable(Decimal(38, 0)),
    betting_users_derived Nullable(Decimal(38, 0)),
    player_bet_count_per_user Nullable(Decimal(38, 18)),
    player_avg_bet_amount Nullable(Decimal(38, 18)),
    player_bet_amount_per_user Nullable(Decimal(38, 18)),
    robot_cash_lost_raw Nullable(Decimal(38, 12))
)
ENGINE = MergeTree
ORDER BY (snapshot_id, load_revision, business_date, channel, service_scope, gameplay);

CREATE TABLE IF NOT EXISTS __BUSINESS_EVENTS_TABLE__
(
    snapshot_id String,
    load_revision String,
    source_family LowCardinality(String),
    event_id String,
    event_type String,
    event_start_date Date,
    event_end_date Date,
    affected_scope String,
    authority String,
    evidence_level String,
    wording_limit String,
    recurrence_kind String,
    recurrence_month_start UInt8,
    recurrence_day_start UInt8,
    recurrence_month_end UInt8,
    recurrence_day_end UInt8,
    payload String
)
ENGINE = MergeTree
ORDER BY (snapshot_id, load_revision, source_family, event_id);
-- END GAMEPLAY_EVENTS
