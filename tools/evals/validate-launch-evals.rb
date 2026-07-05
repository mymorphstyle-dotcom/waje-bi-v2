#!/usr/bin/env ruby
# frozen_string_literal: true

require "date"
require "set"
require "yaml"

ROOT = File.expand_path("../..", __dir__)
EVAL_PATH = File.join(ROOT, "evals/launch/expectation-packages.yaml")

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
SOURCE_POOLS = %w[real_user_questions historical_failure_cases matrix_generated_boundary_cases].freeze
BUSINESS_FAILURE_TYPES = %w[
  wrong_question_family
  wrong_scope
  wrong_baseline
  missed_key_factor
  over_strong_weak_evidence
  hidden_data_gap
  misleading_visualization
  unsupported_main_conclusion
  permission_leak
].freeze
SYSTEM_POINTS = %w[
  LLM_reasoner
  graph_compiler
  semantic_compiler
  capability_execution
  capability_API
  evidence_reducer
  answer_synthesizer
  answer_verifier
  visualization_planner
  permission_policy
].freeze

def list(value)
  value.is_a?(Array) ? value : []
end

def blank?(value)
  value.nil? || (value.respond_to?(:empty?) && value.empty?)
end

def require_in(errors, owner, label, value, allowed)
  return if allowed.include?(value)

  errors << "#{owner}: invalid #{label} #{value.inspect}"
end

def require_present(errors, owner, label, value)
  errors << "#{owner}: missing #{label}" if blank?(value)
end

errors = []

eval_doc = YAML.safe_load(File.read(EVAL_PATH), permitted_classes: [Date], aliases: false)
support_doc = YAML.safe_load(File.read(File.join(ROOT, "contracts/ledger/capability-support.yaml")), aliases: false)
backlog_doc = YAML.safe_load(File.read(File.join(ROOT, "contracts/backlog/missing-contracts.yaml")), aliases: false)

question_families = support_doc.fetch("question_families").to_set
capabilities = support_doc.fetch("capabilities").to_set
support_ids = support_doc.fetch("support_records").map { |record| record.fetch("support_id") }.to_set
backlog_ids = backlog_doc.fetch("backlog").map { |record| record.fetch("backlog_id") }.to_set

packages = list(eval_doc["expectation_packages"])
errors << "expectation_packages: must not be empty" if packages.empty?

covered_families = Set.new
covered_outcomes = Set.new
covered_pools = Set.new

packages.each do |pkg|
  owner = pkg["case_id"] || "(missing case_id)"
  require_present(errors, owner, "title", pkg["title"])
  require_present(errors, owner, "natural_user_wording", pkg["natural_user_wording"])
  require_in(errors, owner, "source_pool", pkg["source_pool"], SOURCE_POOLS)
  covered_pools << pkg["source_pool"]

  family = pkg["expected_question_family"]
  require_in(errors, owner, "expected_question_family", family, question_families)
  covered_families << family

  list(pkg["merged_question_families"]).each do |merged|
    require_in(errors, owner, "merged_question_family", merged, question_families)
  end

  (list(pkg["required_capabilities"]) + list(pkg["optional_capabilities"]) + list(pkg["forbidden_capabilities"])).each do |capability|
    require_in(errors, owner, "capability", capability, capabilities)
  end

  list(pkg["expected_compiler_actions"]).each do |action|
    outcome = action["outcome"]
    require_in(errors, owner, "compiler outcome", outcome, COMPILER_OUTCOMES)
    covered_outcomes << outcome
  end

  list(pkg["expected_evidence_contract_states"]).each do |state|
    state_owner = "#{owner}/#{state["support_id"] || "missing_support"}"
    require_in(errors, state_owner, "support_id", state["support_id"], support_ids)
    require_in(errors, state_owner, "business_evidence_state", state["business_evidence_state"], BUSINESS_STATES)
    require_in(errors, state_owner, "data_contract_state", state["data_contract_state"], DATA_STATES)
    require_in(errors, state_owner, "evidence_type", state["evidence_type"], EVIDENCE_TYPES)
    require_in(errors, state_owner, "strength", state["strength"], STRENGTHS)
    require_in(errors, state_owner, "wording_limit", state["wording_limit"], WORDING_LIMITS)
    list(state["backlog_refs"]).each do |ref|
      require_in(errors, state_owner, "backlog_ref", ref, backlog_ids)
    end
  end

  list(pkg["allowed_claims"]).each do |claim|
    claim_owner = "#{owner}/#{claim["claim_type"] || "missing_claim_type"}"
    require_in(errors, claim_owner, "evidence_type", claim["evidence_type"], EVIDENCE_TYPES)
    require_in(errors, claim_owner, "strength", claim["strength"], STRENGTHS)
    require_in(errors, claim_owner, "wording_limit", claim["wording_limit"], WORDING_LIMITS)
  end

  attribution = pkg["failure_attribution"] || {}
  list(attribution["business_failure_types"]).each do |value|
    require_in(errors, owner, "business_failure_type", value, BUSINESS_FAILURE_TYPES)
  end
  list(attribution["system_responsibility_points"]).each do |value|
    require_in(errors, owner, "system_responsibility_point", value, SYSTEM_POINTS)
  end
end

missing_families = question_families - covered_families
errors << "missing question family coverage: #{missing_families.to_a.sort.join(", ")}" unless missing_families.empty?

required_outcomes = %w[accept auto_repair targeted_repair degrade block].to_set
missing_outcomes = required_outcomes - covered_outcomes
errors << "missing compiler outcome coverage: #{missing_outcomes.to_a.sort.join(", ")}" unless missing_outcomes.empty?

missing_pools = SOURCE_POOLS.to_set - covered_pools
errors << "missing source pool coverage: #{missing_pools.to_a.sort.join(", ")}" unless missing_pools.empty?

if errors.empty?
  puts "Launch eval validation passed."
  puts "Expectation packages: #{packages.size}"
  puts "Question families: #{covered_families.to_a.sort.join(", ")}"
  puts "Compiler outcomes: #{covered_outcomes.to_a.sort.join(", ")}"
  puts "Source pools: #{covered_pools.to_a.sort.join(", ")}"
else
  warn errors.join("\n")
  exit 1
end
