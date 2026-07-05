#!/usr/bin/env ruby
# frozen_string_literal: true

require "date"
require "set"
require "yaml"

ROOT = File.expand_path("../..", __dir__)
FIXTURE_PATH = File.join(ROOT, "evals/semantic-compiler/semantic-compiler-fixtures.yaml")

DATA_STATES = %w[
  contract_backed
  evidence_linked
  static_assumption
  missing_contract
  permission_limited
  unsupported_grain
  out_of_scope_for_now
].freeze

BUSINESS_STATES = %w[
  quantifiable
  candidate_mechanism
  contextual_evidence
  insufficient
  permission_limited
  unsupported_grain
  out_of_scope
].freeze

EVIDENCE_TYPES = %w[
  accounting_contribution
  statistical_association
  candidate_mechanism
  causal_evidence
  insufficient
].freeze

STRENGTHS = %w[high medium low insufficient].freeze
WORDING_LIMITS = %w[quantified stable_pattern candidate context insufficient blocked].freeze
COMPILER_OUTCOMES = %w[accept auto_repair targeted_repair degrade block skip].freeze
RUNTIME_STATUSES = %w[pending_execution runtime_filled_later degraded blocked].freeze
REQUIRED_DRY_RUN_STATUSES = %w[accept auto_repair targeted_repair degrade block].to_set.freeze

def list(value)
  value.is_a?(Array) ? value : []
end

def blank?(value)
  value.nil? || (value.respond_to?(:empty?) && value.empty?)
end

def rel(path)
  path.delete_prefix("#{ROOT}/")
end

def load_yaml(path, errors)
  YAML.safe_load(File.read(path), permitted_classes: [Date], aliases: false) || {}
rescue Psych::Exception => e
  errors << "#{rel(path)}: YAML parse failed: #{e.message}"
  {}
end

def require_present(errors, owner, label, value)
  errors << "#{owner}: missing #{label}" if blank?(value)
end

def require_in(errors, owner, label, value, allowed)
  return if blank?(value) || allowed.include?(value)

  errors << "#{owner}: invalid #{label} #{value.inspect}"
end

def assert_ref_set(errors, owner, label, actual, expected)
  actual_set = list(actual).to_set
  expected_set = expected.to_set
  return if actual_set == expected_set

  missing = expected_set - actual_set
  extra = actual_set - expected_set
  errors << "#{owner}: #{label} missing #{missing.to_a.sort.join(", ")}" unless missing.empty?
  errors << "#{owner}: #{label} unknown #{extra.to_a.sort.join(", ")}" unless extra.empty?
end

def raw_sensitive_or_external?(query, support)
  fields = list(query.dig("permission_handoff", "blocked_fields"))
  fields.any? { |field| %w[raw_user_id raw_ip raw_device_id unreviewed_raw_external_content].include?(field) } ||
    support&.fetch("data_contract_state", nil) == "out_of_scope_for_now" ||
    query["semantic_query_status"] == "blocked"
end

errors = []

fixture_doc = load_yaml(FIXTURE_PATH, errors)
support_doc = load_yaml(File.join(ROOT, "contracts/ledger/capability-support.yaml"), errors)
backlog_doc = load_yaml(File.join(ROOT, "contracts/backlog/missing-contracts.yaml"), errors)
factor_doc = load_yaml(File.join(ROOT, "contracts/ledger/factor-ledger.yaml"), errors)

support_by_id = list(support_doc["support_records"]).to_h { |record| [record["support_id"], record] }
question_families = list(support_doc["question_families"]).to_set
backlog_ids = list(backlog_doc["backlog"]).map { |record| record["backlog_id"] }.compact.to_set
limitation_ids = list(factor_doc["review_limitations"]).map { |record| record["limitation_id"] }.compact.to_set

dry_runs = list(fixture_doc["dry_run_expected_outputs"])
dry_run_by_fixture = dry_runs.to_h { |dry_run| [dry_run["fixture_id"], dry_run] }
errors << "#{rel(FIXTURE_PATH)}: missing dry_run_expected_outputs" if dry_runs.empty?

covered_outcomes = Set.new

list(fixture_doc["fixtures"]).each do |fixture|
  fixture_id = fixture["fixture_id"]
  owner = "dry-run #{fixture_id || "(missing fixture_id)"}"
  dry_run = dry_run_by_fixture[fixture_id]
  if dry_run.nil?
    errors << "#{owner}: missing dry_run_expected_output"
    next
  end

  require_present(errors, owner, "dry_run_id", dry_run["dry_run_id"])
  require_in(errors, owner, "question_family", fixture["question_family"], question_families)
  require_in(errors, owner, "compiler_outcome", fixture["compiler_outcome"], COMPILER_OUTCOMES)
  covered_outcomes << fixture["compiler_outcome"]

  nodes = list(fixture["accepted_graph_nodes"])
  node_by_id = nodes.to_h { |node| [node["node_id"], node] }
  paths = list(fixture["blocked_degraded_paths"])
  path_by_id = paths.to_h { |path| [path["path_id"], path] }
  requests = list(fixture["semantic_query_requests"])
  request_by_id = requests.to_h { |query| [query["semantic_query_id"], query] }
  responses = list(fixture["semantic_query_responses"])
  response_ids = responses.map { |response| response["semantic_query_id"] }.compact
  envelopes = list(fixture["evidence_envelopes"])
  envelope_by_ref = envelopes.to_h { |envelope| [envelope["evidence_ref"], envelope] }
  claim_group_ids = list(fixture.dig("answer_package_handoff", "claim_group_bindings")).map { |binding| binding["claim_group_id"] }.compact

  assert_ref_set(errors, owner, "input.accepted_graph_node_refs", dry_run.dig("input", "accepted_graph_node_refs"), node_by_id.keys)
  assert_ref_set(errors, owner, "input.blocked_degraded_path_refs", dry_run.dig("input", "blocked_degraded_path_refs"), path_by_id.keys)
  assert_ref_set(errors, owner, "output.semantic_query_request_refs", dry_run.dig("output", "semantic_query_request_refs"), request_by_id.keys)
  assert_ref_set(errors, owner, "output.semantic_query_response_refs", dry_run.dig("output", "semantic_query_response_refs"), response_ids)
  assert_ref_set(errors, owner, "output.evidence_envelope_refs", dry_run.dig("output", "evidence_envelope_refs"), envelope_by_ref.keys)
  assert_ref_set(errors, owner, "output.answer_package_claim_group_refs", dry_run.dig("output", "answer_package_claim_group_refs"), claim_group_ids)

  %w[graph_compiler_provided semantic_compiler_filled runtime_filled_later].each do |field|
    require_present(errors, owner, "field_fill_policy.#{field}", dry_run.dig("field_fill_policy", field))
  end

  list(dry_run["mapping_assertions"]).each do |mapping|
    mapping_owner = "#{owner}/mapping/#{mapping["accepted_graph_node_id"] || "missing_node"}"
    node = node_by_id[mapping["accepted_graph_node_id"]]
    query = request_by_id[mapping["semantic_query_id"]]
    require_present(errors, mapping_owner, "accepted_graph_node_id", mapping["accepted_graph_node_id"])
    require_present(errors, mapping_owner, "semantic_query_id", mapping["semantic_query_id"])
    errors << "#{mapping_owner}: unknown accepted_graph_node_id #{mapping["accepted_graph_node_id"].inspect}" unless node
    errors << "#{mapping_owner}: unknown semantic_query_id #{mapping["semantic_query_id"].inspect}" unless query
    next unless node && query

    errors << "#{mapping_owner}: query accepted_graph_node_id mismatch" unless query["accepted_graph_node_id"] == node["node_id"]
    errors << "#{mapping_owner}: support_id mismatch" unless mapping["support_id"] == node["support_id"] && query["support_id"] == node["support_id"]
    errors << "#{mapping_owner}: response_semantic_query_id does not exist" unless response_ids.include?(mapping["response_semantic_query_id"])
    list(mapping["evidence_refs"]).each do |ref|
      errors << "#{mapping_owner}: unknown evidence_ref #{ref.inspect}" unless envelope_by_ref.key?(ref)
    end
    errors << "#{mapping_owner}: unknown answer_package_claim_group_id #{mapping["answer_package_claim_group_id"].inspect}" unless claim_group_ids.include?(mapping["answer_package_claim_group_id"])
  end

  mapped_nodes = list(dry_run["mapping_assertions"]).map { |mapping| mapping["accepted_graph_node_id"] }.compact.to_set
  missing_mapped_nodes = node_by_id.keys.to_set - mapped_nodes
  errors << "#{owner}: mapping_assertions missing nodes #{missing_mapped_nodes.to_a.sort.join(", ")}" unless missing_mapped_nodes.empty?

  policy_by_query = list(dry_run["query_execution_policy"]).to_h { |policy| [policy["semantic_query_id"], policy] }
  request_by_id.each do |query_id, query|
    query_owner = "#{owner}/query_policy/#{query_id}"
    policy = policy_by_query[query_id]
    if policy.nil?
      errors << "#{query_owner}: missing query_execution_policy"
      next
    end

    require_in(errors, query_owner, "runtime_status", policy["runtime_status"], RUNTIME_STATUSES)
    %w[no_sql no_runtime_connection no_real_result].each do |flag|
      errors << "#{query_owner}: #{flag} must be true" unless policy[flag] == true
    end

    support = support_by_id[query["support_id"]]
    if raw_sensitive_or_external?(query, support)
      errors << "#{query_owner}: blocked/raw-sensitive path must not be executable" unless policy["executable_query_request"] == false
      require_present(errors, query_owner, "block_reason", policy["block_reason"])
    end
  end

  list(fixture["blocked_degraded_paths"]).each do |path|
    path_owner = "#{owner}/path/#{path["path_id"] || "missing_path"}"
    require_present(errors, path_owner, "reason_code", path["reason_code"])
    require_present(errors, path_owner, "business_reason", path["business_reason"])
    require_in(errors, path_owner, "business_evidence_state", path["business_evidence_state"], BUSINESS_STATES)
    require_in(errors, path_owner, "data_contract_state", path["data_contract_state"], DATA_STATES)
    require_in(errors, path_owner, "evidence_type", path["evidence_type"], EVIDENCE_TYPES)
    require_in(errors, path_owner, "strength", path["strength"], STRENGTHS)
    require_in(errors, path_owner, "wording_limit", path["wording_limit"], WORDING_LIMITS)
    list(path["backlog_refs"]).each { |ref| errors << "#{path_owner}: unknown backlog_ref #{ref.inspect}" unless backlog_ids.include?(ref) }
    list(path["limitation_refs"]).each { |ref| errors << "#{path_owner}: unknown limitation_ref #{ref.inspect}" unless limitation_ids.include?(ref) }
  end
end

missing_outcomes = REQUIRED_DRY_RUN_STATUSES - covered_outcomes
errors << "missing dry-run compiler outcome coverage: #{missing_outcomes.to_a.sort.join(", ")}" unless missing_outcomes.empty?

if errors.empty?
  puts "Semantic compiler dry-run fixtures OK"
  puts "Dry-run outputs: #{dry_runs.size}"
  puts "Compiler outcomes: #{covered_outcomes.to_a.sort.join(", ")}"
  exit 0
end

warn "Semantic compiler dry-run validation failed:"
errors.each { |error| warn "- #{error}" }
exit 1
