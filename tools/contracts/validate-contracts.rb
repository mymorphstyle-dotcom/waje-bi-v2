#!/usr/bin/env ruby
# frozen_string_literal: true

require "date"
require "rexml/document"
require "set"
require "yaml"

ROOT = File.expand_path("../..", __dir__)
CONTRACTS = File.join(ROOT, "contracts")

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

CAPABILITIES = %w[
  pattern_scan
  formula_decompose
  joint_attribution
  event_evidence
  outlier_scan
  segment_bridge
  data_quality_check
  answer_verify
].freeze

QUESTION_FAMILIES = %w[
  paid_amount_change_explanation
  pattern_explanation
  business_object_impact_review
  revenue_health_review
  segment_or_factor_attribution
  anomaly_or_black_swan_review
  custom_baseline_comparison
  data_quality_or_evidence_review
].freeze

errors = []

def rel(path)
  path.delete_prefix("#{ROOT}/")
end

def list(value)
  value.is_a?(Array) ? value : []
end

def blank?(value)
  value.nil? || (value.respond_to?(:empty?) && value.empty?)
end

def parse_yaml(path, errors)
  YAML.safe_load(File.read(path), permitted_classes: [Date], aliases: false) || {}
rescue Psych::Exception => e
  errors << "#{rel(path)}: YAML parse failed: #{e.message}"
  {}
end

def require_in(errors, path, label, value, allowed)
  return if blank?(value) || allowed.include?(value)

  errors << "#{rel(path)}: invalid #{label} #{value.inspect}"
end

def require_backlog_refs(errors, path, owner, refs, backlog_ids)
  refs = list(refs)
  if refs.empty?
    errors << "#{rel(path)}: #{owner} missing backlog_refs"
    return
  end

  refs.each do |ref|
    errors << "#{rel(path)}: #{owner} unknown backlog_ref #{ref.inspect}" unless backlog_ids.include?(ref)
  end
end

def walk(value, &block)
  case value
  when Hash
    yield value
    value.each_value { |child| walk(child, &block) }
  when Array
    value.each { |child| walk(child, &block) }
  end
end

yaml_paths = Dir.glob(File.join(CONTRACTS, "**/*.yaml")).sort
docs = yaml_paths.to_h { |path| [rel(path), parse_yaml(path, errors)] }

backlog_doc = docs.fetch("contracts/backlog/missing-contracts.yaml", {})
backlog_entries = list(backlog_doc["backlog"])
backlog_ids = backlog_entries.map { |entry| entry["backlog_id"] }.compact.to_set

docs.each do |path_string, doc|
  path = File.join(ROOT, path_string)
  walk(doc) do |node|
    require_in(errors, path, "data_contract_state", node["data_contract_state"], DATA_STATES) if node.key?("data_contract_state")
    require_in(errors, path, "business_evidence_state", node["business_evidence_state"], BUSINESS_STATES) if node.key?("business_evidence_state")
    require_in(errors, path, "evidence_type", node["evidence_type"], EVIDENCE_TYPES) if node.key?("evidence_type")
    require_in(errors, path, "strength", node["strength"], STRENGTHS) if node.key?("strength")
    require_in(errors, path, "wording_limit", node["wording_limit"], WORDING_LIMITS) if node.key?("wording_limit")
    require_in(errors, path, "allowed_wording_limit", node["allowed_wording_limit"], WORDING_LIMITS) if node.key?("allowed_wording_limit")
    require_in(errors, path, "capability", node["capability"], CAPABILITIES) if node.key?("capability")
    require_in(errors, path, "capability_id", node["capability_id"], CAPABILITIES) if node.key?("capability_id")
    require_in(errors, path, "question_family", node["question_family"], QUESTION_FAMILIES) if node.key?("question_family")
    list(node["allowed_evidence_types"]).each { |value| require_in(errors, path, "allowed_evidence_type", value, EVIDENCE_TYPES) }
    list(node["allowed_wording_limits"]).each { |value| require_in(errors, path, "allowed_wording_limit", value, WORDING_LIMITS) }
    list(node["affected_capabilities"]).each { |value| require_in(errors, path, "affected_capability", value, CAPABILITIES) }
    list(node["capabilities"]).each { |value| require_in(errors, path, "capability", value, CAPABILITIES) if value.is_a?(String) }
    list(node["affected_question_families"]).each { |value| require_in(errors, path, "affected_question_family", value, QUESTION_FAMILIES) }
    list(node["question_families"]).each { |value| require_in(errors, path, "question_family", value, QUESTION_FAMILIES) if value.is_a?(String) }
    list(node["typical_question_families"]).each { |value| require_in(errors, path, "typical_question_family", value, QUESTION_FAMILIES) }
  end
end

backlog_entries.each do |entry|
  path = File.join(ROOT, "contracts/backlog/missing-contracts.yaml")
  owner = "backlog #{entry["backlog_id"] || "(missing id)"}"
  require_in(errors, path, "#{owner} data_contract_state", entry["data_contract_state"], DATA_STATES)
  list(entry["affected_capabilities"]).each do |capability|
    require_in(errors, path, "#{owner} capability", capability, CAPABILITIES)
  end
  list(entry["affected_question_families"]).each do |family|
    require_in(errors, path, "#{owner} question_family", family, QUESTION_FAMILIES)
  end
end

factor_path = File.join(ROOT, "contracts/ledger/factor-ledger.yaml")
factor_doc = docs.fetch("contracts/ledger/factor-ledger.yaml", {})
list(factor_doc["factor_groups"]).each do |group|
  owner = "factor_group #{group["factor_group_id"] || "(missing id)"}"
  require_in(errors, factor_path, "#{owner} data_contract_state", group["data_contract_state"], DATA_STATES)
  require_in(errors, factor_path, "#{owner} business_evidence_state", group["default_business_evidence_state"], BUSINESS_STATES)
  list(group["allowed_evidence_types"]).each do |evidence_type|
    require_in(errors, factor_path, "#{owner} evidence_type", evidence_type, EVIDENCE_TYPES)
  end
  list(group["allowed_wording_limits"]).each do |wording|
    require_in(errors, factor_path, "#{owner} wording_limit", wording, WORDING_LIMITS)
  end
  if group["data_contract_state"] == "missing_contract"
    require_backlog_refs(errors, factor_path, owner, group["known_gaps"], backlog_ids)
  else
    list(group["known_gaps"]).each do |ref|
      errors << "#{rel(factor_path)}: #{owner} unknown known_gap #{ref.inspect}" unless backlog_ids.include?(ref)
    end
  end
  list(group["static_assumptions"]).each do |assumption|
    missing = %w[owner source valid_window refresh_rule].select { |field| blank?(assumption[field]) }
    missing << "wording_limit" if blank?(assumption["wording_limit"]) && blank?(assumption["allowed_wording_limit"])
    next if missing.empty?

    errors << "#{rel(factor_path)}: static_assumption #{assumption["assumption_id"] || "(missing id)"} missing #{missing.join(", ")}"
  end
end

list(factor_doc["review_limitations"]).each do |limitation|
  ref = limitation["backlog_ref"]
  next if blank?(ref) || backlog_ids.include?(ref)

  errors << "#{rel(factor_path)}: limitation #{limitation["limitation_id"]} unknown backlog_ref #{ref.inspect}"
end

support_path = File.join(ROOT, "contracts/ledger/capability-support.yaml")
support_doc = docs.fetch("contracts/ledger/capability-support.yaml", {})
list(support_doc["question_families"]).each do |family|
  require_in(errors, support_path, "question_family", family, QUESTION_FAMILIES)
end
list(support_doc["capabilities"]).each do |capability|
  require_in(errors, support_path, "capability", capability, CAPABILITIES)
end
list(support_doc["support_records"]).each do |record|
  owner = "support_record #{record["support_id"] || "(missing id)"}"
  require_in(errors, support_path, "#{owner} question_family", record["question_family"], QUESTION_FAMILIES)
  require_in(errors, support_path, "#{owner} capability", record["capability"], CAPABILITIES)
  require_in(errors, support_path, "#{owner} data_contract_state", record["data_contract_state"], DATA_STATES)
  require_in(errors, support_path, "#{owner} business_evidence_state", record["business_evidence_state"], BUSINESS_STATES)
  require_in(errors, support_path, "#{owner} evidence_type", record["evidence_type"], EVIDENCE_TYPES)
  require_in(errors, support_path, "#{owner} strength", record["strength"], STRENGTHS)
  require_in(errors, support_path, "#{owner} wording_limit", record["wording_limit"], WORDING_LIMITS)
  require_backlog_refs(errors, support_path, owner, record["backlog_refs"], backlog_ids) if record["data_contract_state"] == "missing_contract"
  list(record["backlog_refs"]).each do |ref|
    errors << "#{rel(support_path)}: #{owner} unknown backlog_ref #{ref.inspect}" unless backlog_ids.include?(ref)
  end
end

ssot_map_path = File.join(ROOT, "contracts/ledger/ssot-node-reconciliation.yaml")
ssot_map_doc = docs.fetch("contracts/ledger/ssot-node-reconciliation.yaml", {})
mapped_node_ids = list(ssot_map_doc["nodes"]).map { |node| node["node_id"] }.compact.to_set
if File.exist?(File.join(ROOT, "contracts/ssot/付费金额影响因子分析.mm"))
  ssot_doc = REXML::Document.new(File.read(File.join(ROOT, "contracts/ssot/付费金额影响因子分析.mm")))
  ssot_node_ids = []
  ssot_doc.elements.each("//node") { |node| ssot_node_ids << node.attributes["ID"] }
  missing_ssot_nodes = ssot_node_ids.compact.to_set - mapped_node_ids
  extra_mapped_nodes = mapped_node_ids - ssot_node_ids.compact.to_set
  errors << "#{rel(ssot_map_path)}: missing SSOT node ids #{missing_ssot_nodes.to_a.sort.join(", ")}" unless missing_ssot_nodes.empty?
  errors << "#{rel(ssot_map_path)}: extra mapped node ids #{extra_mapped_nodes.to_a.sort.join(", ")}" unless extra_mapped_nodes.empty?
end

Dir.glob(File.join(ROOT, "contracts/assumptions/*.yaml")).sort.each do |path|
  doc = docs.fetch(rel(path), {})
  list(doc["assumptions"]).each do |assumption|
    owner = "assumption #{assumption["assumption_id"] || "(missing id)"}"
    require_in(errors, path, "#{owner} data_contract_state", assumption["data_contract_state"], DATA_STATES)
    require_in(errors, path, "#{owner} business_evidence_state", assumption["business_evidence_state"], BUSINESS_STATES)
    missing = %w[owner source valid_window refresh_rule wording_limit].select { |field| blank?(assumption[field]) }
    errors << "#{rel(path)}: #{owner} missing #{missing.join(", ")}" unless missing.empty?
    require_in(errors, path, "#{owner} wording_limit", assumption["wording_limit"], WORDING_LIMITS)
    list(assumption["backlog_refs"]).each do |ref|
      errors << "#{rel(path)}: #{owner} unknown backlog_ref #{ref.inspect}" unless backlog_ids.include?(ref)
    end
  end
end

capability_ids = []
Dir.glob(File.join(ROOT, "contracts/capabilities/*.yaml")).sort.each do |path|
  doc = docs.fetch(rel(path), {})
  capability_id = doc["capability_id"]
  capability_ids << capability_id
  require_in(errors, path, "capability_id", capability_id, CAPABILITIES)
  %w[responsibilities non_uses evidence_outputs limitations degradation_rules lint_rules verifier_hooks typical_question_families].each do |field|
    errors << "#{rel(path)}: missing #{field}" if blank?(doc[field])
  end
  if !doc["parameters"].is_a?(Hash) || blank?(doc.dig("parameters", "required"))
    errors << "#{rel(path)}: missing parameters.required"
  end
  list(doc["typical_question_families"]).each do |family|
    require_in(errors, path, "typical_question_family", family, QUESTION_FAMILIES)
  end
end

missing_cards = CAPABILITIES - capability_ids
extra_cards = capability_ids.compact - CAPABILITIES
errors << "contracts/capabilities: missing capability cards #{missing_cards.join(", ")}" unless missing_cards.empty?
errors << "contracts/capabilities: extra capability cards #{extra_cards.join(", ")}" unless extra_cards.empty?

state_counts = Hash.new(0)
pending_count = 0
gap_ids = Set.new

docs.each do |_path, doc|
  queue = [doc]
  until queue.empty?
    value = queue.pop
    case value
    when Hash
      state_counts[value["data_contract_state"]] += 1 if DATA_STATES.include?(value["data_contract_state"])
      pending_count += 1 if value["review_status"].to_s.include?("pending")
      list(value["known_gaps"]).each do |gap|
        gap_ids << (gap.is_a?(Hash) ? gap["gap_id"] : gap)
      end
      list(value["backlog_refs"]).each { |gap| gap_ids << gap }
      value.each_value { |child| queue << child }
    when Array
      value.each { |child| queue << child }
    end
  end
end

if errors.empty?
  puts "Contract validation passed."
  puts "YAML files parsed: #{yaml_paths.size}"
  puts "Capability cards: #{capability_ids.compact.sort.join(", ")}"
  puts "Support records: #{list(support_doc["support_records"]).size}"
  puts "Pending review markers: #{pending_count}"
  puts "Data contract states: #{state_counts.sort.map { |state, count| "#{state}=#{count}" }.join(", ")}"
  puts "Backlog refs in use: #{gap_ids.sort.join(", ")}"
else
  warn "Contract validation failed with #{errors.size} error(s):"
  errors.each { |error| warn "- #{error}" }
  exit 1
end
