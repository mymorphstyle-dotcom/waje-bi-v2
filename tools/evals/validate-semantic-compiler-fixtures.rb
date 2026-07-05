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
NODE_STATUSES = %w[accepted auto_added repaired degraded blocked skipped repair_requested].freeze
SEMANTIC_QUERY_STATUSES = %w[accepted degraded blocked skipped repair_requested accepted_with_permission_limit].freeze
PATH_STATUSES = %w[disabled degraded blocked skipped].freeze
RECONCILIATION_STATUSES = %w[passed degraded failed not_applicable].freeze
ANSWER_HANDOFF_STATUSES = %w[ready requires_limitation requires_repair blocked].freeze

REQUIRED_OUTCOMES = %w[accept auto_repair targeted_repair degrade block].to_set.freeze
REQUIRED_EVIDENCE_CAPABILITIES = %w[
  pattern_scan
  formula_decompose
  event_evidence
  segment_bridge
  outlier_scan
  data_quality_check
  joint_attribution
].to_set.freeze
REQUIRED_LIMITATION_STATES = %w[
  missing_contract
  permission_limited
  unsupported_grain
  out_of_scope_for_now
].to_set.freeze
REQUIRED_USED_DATA_STATES = %w[
  static_assumption
  missing_contract
  permission_limited
  unsupported_grain
  out_of_scope_for_now
].to_set.freeze

def rel(path)
  path.delete_prefix("#{ROOT}/")
end

def list(value)
  value.is_a?(Array) ? value : []
end

def blank?(value)
  value.nil? || (value.respond_to?(:empty?) && value.empty?)
end

def require_present(errors, owner, label, value)
  errors << "#{owner}: missing #{label}" if blank?(value)
end

def require_in(errors, owner, label, value, allowed)
  return if blank?(value) || allowed.include?(value)

  errors << "#{owner}: invalid #{label} #{value.inspect}"
end

def load_yaml(path, errors)
  unless File.exist?(path)
    errors << "#{rel(path)}: missing fixture file"
    return {}
  end

  YAML.safe_load(File.read(path), permitted_classes: [Date], aliases: false) || {}
rescue Psych::Exception => e
  errors << "#{rel(path)}: YAML parse failed: #{e.message}"
  {}
end

errors = []

support_doc = load_yaml(File.join(ROOT, "contracts/ledger/capability-support.yaml"), errors)
backlog_doc = load_yaml(File.join(ROOT, "contracts/backlog/missing-contracts.yaml"), errors)
factor_doc = load_yaml(File.join(ROOT, "contracts/ledger/factor-ledger.yaml"), errors)
fixture_doc = load_yaml(FIXTURE_PATH, errors)

support_records = list(support_doc["support_records"])
support_by_id = support_records.to_h { |record| [record["support_id"], record] }
question_families = list(support_doc["question_families"]).to_set
capabilities = list(support_doc["capabilities"]).to_set
backlog_ids = list(backlog_doc["backlog"]).map { |record| record["backlog_id"] }.compact.to_set
limitation_ids = list(factor_doc["review_limitations"]).map { |record| record["limitation_id"] }.compact.to_set

typed_payload_by_capability = Dir.glob(File.join(ROOT, "contracts/capabilities/*.yaml")).each_with_object({}) do |path, acc|
  card = load_yaml(path, errors)
  acc[card["capability_id"]] = card.dig("evidence_outputs", "typed_payload")
end

coverage = {
  question_families: Set.new,
  compiler_outcomes: Set.new,
  evidence_capabilities: Set.new,
  limitation_states: Set.new,
  used_data_states: Set.new
}

list(fixture_doc["fixtures"]).each do |fixture|
  fixture_id = fixture["fixture_id"] || "(missing fixture_id)"
  owner = "fixture #{fixture_id}"

  require_present(errors, owner, "fixture_id", fixture["fixture_id"])
  require_present(errors, owner, "launch_case_id", fixture["launch_case_id"])
  require_in(errors, owner, "question_family", fixture["question_family"], question_families)
  require_in(errors, owner, "compiler_outcome", fixture["compiler_outcome"], COMPILER_OUTCOMES)

  coverage[:question_families] << fixture["question_family"]
  coverage[:compiler_outcomes] << fixture["compiler_outcome"]

  paths = list(fixture["blocked_degraded_paths"])
  path_by_id = paths.to_h { |path| [path["path_id"], path] }
  if %w[targeted_repair degrade block].include?(fixture["compiler_outcome"]) && paths.empty?
    errors << "#{owner}: #{fixture["compiler_outcome"]} requires blocked_degraded_paths"
  end

  paths.each do |path|
    path_owner = "#{owner}/path/#{path["path_id"] || "missing_path_id"}"
    require_present(errors, path_owner, "path_id", path["path_id"])
    require_in(errors, path_owner, "path_status", path["path_status"], PATH_STATUSES)
    require_present(errors, path_owner, "reason_code", path["reason_code"])
    require_present(errors, path_owner, "business_reason", path["business_reason"])
    require_in(errors, path_owner, "business_evidence_state", path["business_evidence_state"], BUSINESS_STATES)
    require_in(errors, path_owner, "data_contract_state", path["data_contract_state"], DATA_STATES)
    require_in(errors, path_owner, "evidence_type", path["evidence_type"], EVIDENCE_TYPES)
    require_in(errors, path_owner, "strength", path["strength"], STRENGTHS)
    require_in(errors, path_owner, "wording_limit", path["wording_limit"], WORDING_LIMITS)

    coverage[:limitation_states] << path["data_contract_state"] if REQUIRED_LIMITATION_STATES.include?(path["data_contract_state"])
    coverage[:used_data_states] << path["data_contract_state"] if REQUIRED_USED_DATA_STATES.include?(path["data_contract_state"])

    list(path["backlog_refs"]).each do |ref|
      errors << "#{path_owner}: unknown backlog_ref #{ref.inspect}" unless backlog_ids.include?(ref)
    end
    list(path["limitation_refs"]).each do |ref|
      errors << "#{path_owner}: unknown limitation_ref #{ref.inspect}" unless limitation_ids.include?(ref)
    end
    if list(path["backlog_refs"]).empty? && list(path["limitation_refs"]).empty? && blank?(path["requested_vs_accepted_grain"]) && blank?(path["block_reason"])
      errors << "#{path_owner}: missing backlog_refs, limitation_refs, requested_vs_accepted_grain, or block_reason"
    end
  end

  list(fixture["accepted_graph_nodes"]).each do |node|
    node_owner = "#{owner}/node/#{node["node_id"] || "missing_node_id"}"
    require_present(errors, node_owner, "node_id", node["node_id"])
    require_in(errors, node_owner, "status", node["status"], NODE_STATUSES)
    require_in(errors, node_owner, "question_family", node["question_family"], question_families)
    require_in(errors, node_owner, "capability", node["capability"], capabilities)
    if support_by_id.key?(node["support_id"])
      support = support_by_id[node["support_id"]]
      errors << "#{node_owner}: capability does not match support_id" unless support["capability"] == node["capability"]
      errors << "#{node_owner}: question_family does not match support_id" unless support["question_family"] == node["question_family"]
      coverage[:used_data_states] << support["data_contract_state"] if REQUIRED_USED_DATA_STATES.include?(support["data_contract_state"])
    else
      errors << "#{node_owner}: unknown support_id #{node["support_id"].inspect}"
    end
  end

  query_ids = list(fixture["semantic_query_requests"]).map { |query| query["semantic_query_id"] }.compact.to_set
  list(fixture["semantic_query_requests"]).each do |query|
    query_owner = "#{owner}/query/#{query["semantic_query_id"] || "missing_query_id"}"
    require_present(errors, query_owner, "semantic_query_id", query["semantic_query_id"])
    require_in(errors, query_owner, "semantic_query_status", query["semantic_query_status"], SEMANTIC_QUERY_STATUSES)
    require_in(errors, query_owner, "question_family", query["question_family"], question_families)
    require_in(errors, query_owner, "capability", query["capability"], capabilities)
    require_present(errors, query_owner, "contract_version_pins_required", query["contract_version_pins_required"])
    require_present(errors, query_owner, "current_data_snapshot_binding", query["current_data_snapshot_binding"])
    require_present(errors, query_owner, "permission_handoff", query["permission_handoff"])
    require_present(errors, query_owner, "data_quality_handoff", query["data_quality_handoff"])
    require_present(errors, query_owner, "guard_handoff", query["guard_handoff"])
    require_present(errors, query_owner, "expected_evidence_contract", query["expected_evidence_contract"])

    support = support_by_id[query["support_id"]]
    if support
      errors << "#{query_owner}: capability does not match support_id" unless support["capability"] == query["capability"]
      errors << "#{query_owner}: question_family does not match support_id" unless support["question_family"] == query["question_family"]
      coverage[:used_data_states] << support["data_contract_state"] if REQUIRED_USED_DATA_STATES.include?(support["data_contract_state"])
    else
      errors << "#{query_owner}: unknown support_id #{query["support_id"].inspect}"
    end

    list(query["disabled_degraded_blocked_path_refs"]).each do |ref|
      errors << "#{query_owner}: unknown path ref #{ref.inspect}" unless path_by_id.key?(ref)
    end
    if %w[degraded blocked repair_requested accepted_with_permission_limit].include?(query["semantic_query_status"]) &&
       list(query["disabled_degraded_blocked_path_refs"]).empty?
      errors << "#{query_owner}: #{query["semantic_query_status"]} requires disabled_degraded_blocked_path_refs"
    end
  end

  list(fixture["semantic_query_responses"]).each do |response|
    response_owner = "#{owner}/response/#{response["semantic_query_id"] || "missing_query_id"}"
    errors << "#{response_owner}: semantic_query_id does not match request" unless query_ids.include?(response["semantic_query_id"])
    require_present(errors, response_owner, "semantic_plan_summary", response["semantic_plan_summary"])
    require_present(errors, response_owner, "metric_refs", response["metric_refs"])
    require_present(errors, response_owner, "source_refs", response["source_refs"])
    require_present(errors, response_owner, "result_shape", response["result_shape"])
    require_in(errors, response_owner, "numeric_reconciliation_status", response["numeric_reconciliation_status"], RECONCILIATION_STATUSES)
    require_in(errors, response_owner, "permission_status", response["permission_status"], DATA_STATES)
    require_present(errors, response_owner, "evidence_handoff_refs", response["evidence_handoff_refs"])
  end

  evidence_refs = list(fixture["evidence_envelopes"]).map { |envelope| envelope["evidence_ref"] }.compact.to_set
  list(fixture["evidence_envelopes"]).each do |envelope|
    envelope_owner = "#{owner}/evidence/#{envelope["evidence_ref"] || "missing_evidence_ref"}"
    require_present(errors, envelope_owner, "evidence_ref", envelope["evidence_ref"])
    errors << "#{envelope_owner}: semantic_query_id does not match request" unless query_ids.include?(envelope["semantic_query_id"])
    require_in(errors, envelope_owner, "capability", envelope["capability"], capabilities)
    require_in(errors, envelope_owner, "evidence_type", envelope["evidence_type"], EVIDENCE_TYPES)
    require_in(errors, envelope_owner, "strength", envelope["strength"], STRENGTHS)
    require_in(errors, envelope_owner, "wording_limit", envelope["wording_limit"], WORDING_LIMITS)
    require_present(errors, envelope_owner, "limitations", envelope["limitations"])
    require_present(errors, envelope_owner, "numeric_reconciliation", envelope["numeric_reconciliation"])
    require_present(errors, envelope_owner, "verifier_handoff", envelope["verifier_handoff"])

    support = support_by_id[envelope["support_id"]]
    if support
      errors << "#{envelope_owner}: capability does not match support_id" unless support["capability"] == envelope["capability"]
      coverage[:used_data_states] << support["data_contract_state"] if REQUIRED_USED_DATA_STATES.include?(support["data_contract_state"])
    else
      errors << "#{envelope_owner}: unknown support_id #{envelope["support_id"].inspect}"
    end

    expected_payload = typed_payload_by_capability[envelope["capability"]]
    if expected_payload && envelope["typed_payload"] != expected_payload
      errors << "#{envelope_owner}: typed_payload #{envelope["typed_payload"].inspect} should be #{expected_payload.inspect}"
    end

    coverage[:evidence_capabilities] << envelope["capability"]

    reconciliation = envelope["numeric_reconciliation"] || {}
    require_in(errors, envelope_owner, "numeric_reconciliation.reconciliation_status", reconciliation["reconciliation_status"], RECONCILIATION_STATUSES)

    list(envelope["limitations"]).each do |limitation|
      limitation_owner = "#{envelope_owner}/limitation/#{limitation["limitation_id"] || "missing_limitation_id"}"
      require_present(errors, limitation_owner, "limitation_id", limitation["limitation_id"])
      require_in(errors, limitation_owner, "limitation_type", limitation["limitation_type"], DATA_STATES + BUSINESS_STATES)
      coverage[:limitation_states] << limitation["limitation_type"] if REQUIRED_LIMITATION_STATES.include?(limitation["limitation_type"])
      coverage[:used_data_states] << limitation["limitation_type"] if REQUIRED_USED_DATA_STATES.include?(limitation["limitation_type"])
      list(limitation["backlog_refs"]).each do |ref|
        errors << "#{limitation_owner}: unknown backlog_ref #{ref.inspect}" unless backlog_ids.include?(ref)
      end
      list(limitation["limitation_refs"]).each do |ref|
        errors << "#{limitation_owner}: unknown limitation_ref #{ref.inspect}" unless limitation_ids.include?(ref)
      end
    end

    list(envelope["disabled_degraded_blocked_path_refs"]).each do |ref|
      errors << "#{envelope_owner}: unknown path ref #{ref.inspect}" unless path_by_id.key?(ref)
    end
  end

  response_evidence_refs = list(fixture["semantic_query_responses"]).flat_map { |response| list(response["evidence_handoff_refs"]) }
  response_evidence_refs.each do |ref|
    errors << "#{owner}: response references unknown evidence_ref #{ref.inspect}" unless evidence_refs.include?(ref)
  end

  handoff = fixture["answer_package_handoff"] || {}
  handoff_owner = "#{owner}/answer_package_handoff"
  require_in(errors, handoff_owner, "handoff_status", handoff["handoff_status"], ANSWER_HANDOFF_STATUSES)
  require_present(errors, handoff_owner, "claim_group_bindings", handoff["claim_group_bindings"])
  list(handoff["claim_group_bindings"]).each do |binding|
    binding_owner = "#{handoff_owner}/#{binding["claim_group_id"] || "missing_claim_group_id"}"
    list(binding["required_evidence_refs"]).each do |ref|
      errors << "#{binding_owner}: unknown required_evidence_ref #{ref.inspect}" unless evidence_refs.include?(ref)
    end
    list(binding["disabled_degraded_blocked_path_refs"]).each do |ref|
      errors << "#{binding_owner}: unknown path ref #{ref.inspect}" unless path_by_id.key?(ref)
    end
  end
end

if list(fixture_doc["fixtures"]).empty?
  errors << "#{rel(FIXTURE_PATH)}: missing fixtures"
else
  missing_families = question_families - coverage[:question_families]
  errors << "missing question family coverage: #{missing_families.to_a.sort.join(", ")}" unless missing_families.empty?

  missing_outcomes = REQUIRED_OUTCOMES - coverage[:compiler_outcomes]
  errors << "missing compiler outcome coverage: #{missing_outcomes.to_a.sort.join(", ")}" unless missing_outcomes.empty?

  missing_capabilities = REQUIRED_EVIDENCE_CAPABILITIES - coverage[:evidence_capabilities]
  errors << "missing evidence capability coverage: #{missing_capabilities.to_a.sort.join(", ")}" unless missing_capabilities.empty?

  missing_limitation_states = REQUIRED_LIMITATION_STATES - coverage[:limitation_states]
  errors << "missing limitation state coverage: #{missing_limitation_states.to_a.sort.join(", ")}" unless missing_limitation_states.empty?

  missing_data_states = REQUIRED_USED_DATA_STATES - coverage[:used_data_states]
  errors << "missing used data_contract_state coverage: #{missing_data_states.to_a.sort.join(", ")}" unless missing_data_states.empty?
end

if errors.empty?
  puts "Semantic compiler fixtures OK"
  puts "Fixtures: #{list(fixture_doc["fixtures"]).size}"
  puts "Question families: #{coverage[:question_families].to_a.sort.join(", ")}"
  puts "Compiler outcomes: #{coverage[:compiler_outcomes].to_a.sort.join(", ")}"
  exit 0
end

warn "Semantic compiler fixture validation failed:"
errors.each { |error| warn "- #{error}" }
exit 1
