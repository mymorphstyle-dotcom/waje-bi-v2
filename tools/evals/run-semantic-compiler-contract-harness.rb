#!/usr/bin/env ruby
# frozen_string_literal: true

require "date"
require "open3"
require "rbconfig"
require "set"
require "yaml"

ROOT = File.expand_path("../..", __dir__)
FIXTURE_PATH = ENV.fetch("WAJE_SEMANTIC_COMPILER_FIXTURES", File.join(ROOT, "evals/semantic-compiler/semantic-compiler-fixtures.yaml"))
GENERATOR_PATH = File.join(ROOT, "tools/evals/generate-semantic-compiler-dry-run.rb")

REQUIRED_OUTCOMES = %w[accept auto_repair targeted_repair degrade block].to_set.freeze
RAW_BLOCKED_FIELDS = %w[raw_user_id raw_ip raw_device_id unreviewed_raw_external_content].freeze
REQUIRED_NOTICE_PHRASES = [
  "not runtime output",
  "no sql",
  "no database",
  "no capability execution",
  "no real business conclusion"
].freeze
NON_RUNTIME_NOTICE = "Contract harness only; not runtime output; no SQL; no database query; no capability execution; no real typed payload values; no real business conclusion; --print writes inspection YAML to stdout only."

def list(value)
  value.is_a?(Array) ? value : []
end

def blank?(value)
  value.nil? || (value.respond_to?(:empty?) && value.empty?)
end

def load_yaml(path)
  YAML.safe_load(File.read(path), permitted_classes: [Date], aliases: false) || {}
end

def generated_dry_runs
  output, status = Open3.capture2e(RbConfig.ruby, GENERATOR_PATH, "--print")
  raise "dry-run generator failed:\n#{output}" unless status.success?

  YAML.safe_load(output, permitted_classes: [Date], aliases: true).fetch("dry_run_expected_outputs")
end

def raw_or_blocked?(query, support)
  fields = list(query.dig("permission_handoff", "blocked_fields"))

  query["semantic_query_status"] == "blocked" ||
    fields.any? { |field| RAW_BLOCKED_FIELDS.include?(field) } ||
    support&.fetch("data_contract_state", nil) == "out_of_scope_for_now"
end

def evidence_refs_from_handoff(handoff)
  list(handoff["claim_group_bindings"]).flat_map { |binding| list(binding["required_evidence_refs"]) }.compact.to_set
end

def fixture_support_ids(fixture)
  list(fixture["accepted_graph_nodes"]).map { |node| node["support_id"] } +
    list(fixture["semantic_query_requests"]).map { |query| query["support_id"] } +
    list(fixture["evidence_envelopes"]).map { |envelope| envelope["support_id"] }
end

def fixture_backlog_refs(fixture)
  path_refs = list(fixture["blocked_degraded_paths"]).flat_map { |path| list(path["backlog_refs"]) }
  limitation_refs = list(fixture["evidence_envelopes"]).flat_map do |envelope|
    list(envelope["limitations"]).flat_map { |limitation| list(limitation["backlog_refs"]) }
  end

  (path_refs + limitation_refs).compact
end

def fixture_limitation_refs(fixture)
  path_refs = list(fixture["blocked_degraded_paths"]).flat_map { |path| list(path["limitation_refs"]) }
  limitation_refs = list(fixture["evidence_envelopes"]).flat_map do |envelope|
    list(envelope["limitations"]).flat_map { |limitation| list(limitation["limitation_refs"]) }
  end

  (path_refs + limitation_refs).compact
end

def validation_summary(fixture, dry_run)
  policies = list(dry_run["query_execution_policy"])
  blocked = policies.select { |policy| policy["runtime_status"] == "blocked" }
  degraded = policies.select { |policy| policy["runtime_status"] == "degraded" }

  {
    "dry_run_id" => dry_run["dry_run_id"],
    "dry_run_contract_scope" => dry_run["contract_scope"],
    "field_fill_policy" => dry_run["field_fill_policy"],
    "query_execution_policy" => policies,
    "mapping_assertions" => dry_run["mapping_assertions"],
    "query_count" => list(fixture["semantic_query_requests"]).size,
    "response_count" => list(fixture["semantic_query_responses"]).size,
    "evidence_count" => list(fixture["evidence_envelopes"]).size,
    "path_record_count" => list(fixture["blocked_degraded_paths"]).size,
    "non_executable_query_ids" => policies.select { |policy| policy["executable_query_request"] == false }.map { |policy| policy["semantic_query_id"] },
    "degraded_query_ids" => degraded.map { |policy| policy["semantic_query_id"] },
    "blocked_query_ids" => blocked.map { |policy| policy["semantic_query_id"] },
    "support_ids" => fixture_support_ids(fixture).compact.uniq.sort,
    "backlog_refs" => fixture_backlog_refs(fixture).uniq.sort,
    "limitation_refs" => fixture_limitation_refs(fixture).uniq.sort,
    "checks" => {
      "dry_run_refs_match_fixture_sections" => true,
      "blocked_queries_are_non_executable" => true,
      "degraded_queries_preserve_paths_limitations_and_wording_limits" => true,
      "evidence_refs_enter_answer_package_handoff" => true,
      "support_backlog_and_limitation_refs_are_traceable" => true,
      "non_runtime_notice_present" => true
    }
  }
end

def build_bundle(fixture, dry_run, shared_contract_pins)
  {
    "fixture_id" => fixture.fetch("fixture_id"),
    "question_family" => fixture.fetch("question_family"),
    "compiler_outcome" => fixture.fetch("compiler_outcome"),
    "accepted_graph_input" => {
      "launch_case_id" => fixture["launch_case_id"],
      "contract_pins" => shared_contract_pins,
      "accepted_graph_nodes" => list(fixture["accepted_graph_nodes"])
    },
    "semantic_query_request" => list(fixture["semantic_query_requests"]),
    "semantic_query_response_skeleton" => list(fixture["semantic_query_responses"]),
    "evidence_envelopes" => list(fixture["evidence_envelopes"]),
    "answer_package_handoff" => fixture["answer_package_handoff"] || {},
    "path_records" => list(fixture["blocked_degraded_paths"]),
    "validation_summary" => validation_summary(fixture, dry_run),
    "non_runtime_notice" => NON_RUNTIME_NOTICE
  }
end

def validate_bundle!(bundle, support_by_id, backlog_ids, limitation_ids)
  errors = []
  owner = "bundle #{bundle["fixture_id"]}"
  requests = list(bundle["semantic_query_request"])
  policies = list(bundle.dig("validation_summary", "query_execution_policy"))
  policy_by_id = policies.to_h { |policy| [policy["semantic_query_id"], policy] }
  envelopes = list(bundle["evidence_envelopes"])
  envelopes_by_query_id = envelopes.group_by { |envelope| envelope["semantic_query_id"] }
  path_records = list(bundle["path_records"])

  explicit_block_reason = policies.any? { |policy| !blank?(policy["block_reason"]) }
  errors << "#{owner}: missing semantic_query_request or explicit blocked reason" if requests.empty? && !explicit_block_reason

  requests.each do |query|
    policy = policy_by_id[query["semantic_query_id"]]
    support = support_by_id[query["support_id"]]
    errors << "#{owner}/#{query["semantic_query_id"]}: missing query execution policy" unless policy
    next unless policy

    if raw_or_blocked?(query, support) && policy["executable_query_request"] != false
      errors << "#{owner}/#{query["semantic_query_id"]}: blocked/raw-sensitive query is executable"
    end

    next unless policy["runtime_status"] == "degraded"

    query_limit = query.dig("expected_evidence_contract", "wording_limit")
    query_path_refs = list(query["disabled_degraded_blocked_path_refs"])
    query_envelopes = list(envelopes_by_query_id[query["semantic_query_id"]])
    envelope_has_limit = query_envelopes.any? do |envelope|
      !blank?(envelope["wording_limit"]) && !list(envelope["limitations"]).empty?
    end

    if path_records.empty? || query_path_refs.empty? || blank?(query_limit) || !envelope_has_limit
      errors << "#{owner}/#{query["semantic_query_id"]}: degraded query missing path records, limitations, or wording limit"
    end
  end

  if bundle["compiler_outcome"] == "block" && policies.none? { |policy| policy["executable_query_request"] == false }
    errors << "#{owner}: block outcome has no non-executable query policy"
  end

  evidence_refs = envelopes.map { |envelope| envelope["evidence_ref"] }.compact.to_set
  handoff_refs = evidence_refs_from_handoff(bundle["answer_package_handoff"] || {})
  missing_handoff_refs = evidence_refs - handoff_refs
  errors << "#{owner}: evidence refs missing from Answer Package handoff #{missing_handoff_refs.to_a.sort.join(", ")}" unless missing_handoff_refs.empty?

  list(bundle["semantic_query_response_skeleton"]).each do |response|
    list(response["evidence_handoff_refs"]).each do |ref|
      errors << "#{owner}/#{response["semantic_query_id"]}: response evidence ref #{ref.inspect} missing from Answer Package handoff" unless handoff_refs.include?(ref)
    end
  end

  fixture_support_ids({ "accepted_graph_nodes" => bundle.dig("accepted_graph_input", "accepted_graph_nodes"), "semantic_query_requests" => requests, "evidence_envelopes" => envelopes }).compact.uniq.each do |support_id|
    errors << "#{owner}: unknown support_id #{support_id.inspect}" unless support_by_id.key?(support_id)
  end

  fixture_backlog_refs({ "blocked_degraded_paths" => path_records, "evidence_envelopes" => envelopes }).uniq.each do |ref|
    errors << "#{owner}: unknown backlog_ref #{ref.inspect}" unless backlog_ids.include?(ref)
  end

  fixture_limitation_refs({ "blocked_degraded_paths" => path_records, "evidence_envelopes" => envelopes }).uniq.each do |ref|
    errors << "#{owner}: unknown limitation_ref #{ref.inspect}" unless limitation_ids.include?(ref)
  end

  notice = bundle["non_runtime_notice"].to_s.downcase
  REQUIRED_NOTICE_PHRASES.each do |phrase|
    errors << "#{owner}: non_runtime_notice missing #{phrase.inspect}" unless notice.include?(phrase)
  end

  errors
end

fixture_doc = load_yaml(FIXTURE_PATH)
fixtures = list(fixture_doc["fixtures"])
dry_run_by_fixture = generated_dry_runs.to_h { |dry_run| [dry_run["fixture_id"], dry_run] }
support_doc = load_yaml(File.join(ROOT, "contracts/ledger/capability-support.yaml"))
backlog_doc = load_yaml(File.join(ROOT, "contracts/backlog/missing-contracts.yaml"))
factor_doc = load_yaml(File.join(ROOT, "contracts/ledger/factor-ledger.yaml"))

support_by_id = list(support_doc["support_records"]).to_h { |record| [record["support_id"], record] }
backlog_ids = list(backlog_doc["backlog"]).map { |record| record["backlog_id"] }.compact.to_set
limitation_ids = list(factor_doc["review_limitations"]).map { |record| record["limitation_id"] }.compact.to_set

bundles = fixtures.map do |fixture|
  dry_run = dry_run_by_fixture.fetch(fixture.fetch("fixture_id"))
  build_bundle(fixture, dry_run, list(fixture_doc["shared_contract_pins"]))
end

errors = []
errors << "expected 8 bundles, generated #{bundles.size}" unless bundles.size == 8

missing_outcomes = REQUIRED_OUTCOMES - bundles.map { |bundle| bundle["compiler_outcome"] }.compact.to_set
errors << "missing compiler outcome coverage: #{missing_outcomes.to_a.sort.join(", ")}" unless missing_outcomes.empty?

bundles.each do |bundle|
  errors.concat(validate_bundle!(bundle, support_by_id, backlog_ids, limitation_ids))
end

if errors.empty? && ARGV.include?("--print")
  puts YAML.dump("semantic_compiler_contract_bundles" => bundles)
  exit 0
end

if errors.empty?
  puts "Semantic compiler contract harness OK"
  puts "Bundles: #{bundles.size}"
  puts "Compiler outcomes: #{bundles.map { |bundle| bundle["compiler_outcome"] }.uniq.sort.join(", ")}"
  puts "Non-executable queries: #{bundles.sum { |bundle| list(bundle.dig("validation_summary", "non_executable_query_ids")).size }}"
  exit 0
end

warn "Semantic compiler contract harness failed:"
errors.each { |error| warn "- #{error}" }
exit 1
