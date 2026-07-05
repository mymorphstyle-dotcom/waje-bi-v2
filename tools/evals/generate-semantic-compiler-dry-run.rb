#!/usr/bin/env ruby
# frozen_string_literal: true

require "date"
require "json"
require "set"
require "yaml"

ROOT = File.expand_path("../..", __dir__)
FIXTURE_PATH = ENV.fetch("WAJE_SEMANTIC_COMPILER_FIXTURES", File.join(ROOT, "evals/semantic-compiler/semantic-compiler-fixtures.yaml"))

REQUIRED_OUTCOMES = %w[accept auto_repair targeted_repair degrade block].to_set.freeze
RAW_BLOCKED_FIELDS = %w[raw_user_id raw_ip raw_device_id unreviewed_raw_external_content].freeze
SEMANTIC_FILLED = %w[
  semantic_query_request_skeleton
  semantic_query_response_skeleton
  evidence_envelope_skeleton
  answer_package_handoff_skeleton
].freeze
RUNTIME_FILLED = %w[
  executable_sql
  query_result_rows
  capability_typed_payload_values
].freeze

def list(value)
  value.is_a?(Array) ? value : []
end

def load_yaml(path)
  YAML.safe_load(File.read(path), permitted_classes: [Date], aliases: false) || {}
end

def support_by_id
  @support_by_id ||= begin
    path = File.join(ROOT, "contracts/ledger/capability-support.yaml")
    list(load_yaml(path)["support_records"]).to_h { |record| [record["support_id"], record] }
  end
end

def ref_list(records, key)
  list(records).map { |record| record[key] }.compact
end

def dry_run_id(fixture_id)
  "dr_#{fixture_id.downcase.delete("-")}"
end

def raw_or_blocked?(query)
  fields = list(query.dig("permission_handoff", "blocked_fields"))
  support = support_by_id[query["support_id"]]

  query["semantic_query_status"] == "blocked" ||
    fields.any? { |field| RAW_BLOCKED_FIELDS.include?(field) } ||
    support&.fetch("data_contract_state", nil) == "out_of_scope_for_now"
end

def runtime_status(query, executable)
  return "blocked" unless executable

  %w[degraded repair_requested].include?(query["semantic_query_status"]) ? "degraded" : "pending_execution"
end

def block_reason(query)
  fields = list(query.dig("permission_handoff", "blocked_fields"))
  return "raw_external_ingestion_out_of_scope" if query["support_id"] == "event_black_swan_raw_external_ingestion_scope"
  return "raw_identifier_output_and_individual_user_claims_stay_blocked" if query["support_id"] == "dq_permission_sensitive_identifiers"
  return "raw_identifier_or_sensitive_fine_grain_output_blocked" if fields.any? { |field| %w[raw_user_id raw_ip raw_device_id].include?(field) }
  return "raw_external_ingestion_out_of_scope" if fields.include?("unreviewed_raw_external_content")
  return query["semantic_query_status"] if query["semantic_query_status"] == "blocked"

  support_by_id[query["support_id"]]&.fetch("data_contract_state", "blocked")
end

def query_policy(query)
  executable = !raw_or_blocked?(query)
  policy = {
    "semantic_query_id" => query.fetch("semantic_query_id"),
    "executable_query_request" => executable,
    "runtime_status" => runtime_status(query, executable)
  }
  policy["block_reason"] = block_reason(query) unless executable
  policy.merge(
    "no_sql" => true,
    "no_runtime_connection" => true,
    "no_real_result" => true
  )
end

def evidence_refs_for_query(envelopes, query_id)
  list(envelopes).select { |envelope| envelope["semantic_query_id"] == query_id }.map { |envelope| envelope["evidence_ref"] }.compact
end

def claim_group_for_evidence(answer_handoff, evidence_refs)
  list(answer_handoff.dig("claim_group_bindings")).find do |binding|
    (list(binding["required_evidence_refs"]) & evidence_refs).any?
  end&.fetch("claim_group_id", nil)
end

def mapping_assertion(node, fixture)
  query = list(fixture["semantic_query_requests"]).find { |candidate| candidate["accepted_graph_node_id"] == node["node_id"] }
  raise "#{fixture["fixture_id"]}/#{node["node_id"]}: missing semantic query request" unless query

  evidence_refs = evidence_refs_for_query(fixture["evidence_envelopes"], query["semantic_query_id"])
  {
    "accepted_graph_node_id" => node.fetch("node_id"),
    "semantic_query_id" => query.fetch("semantic_query_id"),
    "response_semantic_query_id" => query.fetch("semantic_query_id"),
    "evidence_refs" => evidence_refs,
    "answer_package_claim_group_id" => claim_group_for_evidence(fixture["answer_package_handoff"], evidence_refs),
    "support_id" => node.fetch("support_id")
  }
end

def generated_output(fixture)
  paths = list(fixture["blocked_degraded_paths"])
  {
    "fixture_id" => fixture.fetch("fixture_id"),
    "dry_run_id" => dry_run_id(fixture.fetch("fixture_id")),
    "contract_scope" => "semantic_compiler_mapping_only",
    "input" => {
      "accepted_graph_node_refs" => ref_list(fixture["accepted_graph_nodes"], "node_id"),
      "blocked_degraded_path_refs" => ref_list(paths, "path_id")
    },
    "output" => {
      "semantic_query_request_refs" => ref_list(fixture["semantic_query_requests"], "semantic_query_id"),
      "semantic_query_response_refs" => ref_list(fixture["semantic_query_responses"], "semantic_query_id"),
      "evidence_envelope_refs" => ref_list(fixture["evidence_envelopes"], "evidence_ref"),
      "answer_package_claim_group_refs" => ref_list(fixture.dig("answer_package_handoff", "claim_group_bindings"), "claim_group_id")
    },
    "field_fill_policy" => {
      "graph_compiler_provided" => paths.empty? ? ["accepted_graph_nodes"] : %w[accepted_graph_nodes blocked_degraded_paths],
      "semantic_compiler_filled" => SEMANTIC_FILLED,
      "runtime_filled_later" => RUNTIME_FILLED
    },
    "query_execution_policy" => list(fixture["semantic_query_requests"]).map { |query| query_policy(query) },
    "mapping_assertions" => list(fixture["accepted_graph_nodes"]).map { |node| mapping_assertion(node, fixture) }
  }
end

def assert_generator_contract!(fixtures, generated)
  errors = []
  errors << "expected 8 dry-run outputs, generated #{generated.size}" unless generated.size == 8

  outcomes = list(fixtures).map { |fixture| fixture["compiler_outcome"] }.compact.to_set
  missing = REQUIRED_OUTCOMES - outcomes
  errors << "missing compiler outcome coverage: #{missing.to_a.sort.join(", ")}" unless missing.empty?

  generated.each do |output|
    fixture = fixtures.find { |candidate| candidate["fixture_id"] == output["fixture_id"] }
    next unless fixture

    list(output["query_execution_policy"]).each do |policy|
      query = list(fixture["semantic_query_requests"]).find { |candidate| candidate["semantic_query_id"] == policy["semantic_query_id"] }
      if query && raw_or_blocked?(query) && policy["executable_query_request"] != false
        errors << "#{output["fixture_id"]}/#{policy["semantic_query_id"]}: blocked path generated executable request"
      end

      next unless policy["runtime_status"] == "degraded"

      unless query && !list(query["disabled_degraded_blocked_path_refs"]).empty? && query.dig("expected_evidence_contract", "wording_limit")
        errors << "#{output["fixture_id"]}/#{policy["semantic_query_id"]}: degraded query missing limitation refs or wording limit"
      end
    end
  end

  return if errors.empty?

  raise errors.join("\n")
end

fixture_doc = load_yaml(FIXTURE_PATH)
fixtures = list(fixture_doc["fixtures"])
generated = fixtures.map { |fixture| generated_output(fixture) }
assert_generator_contract!(fixtures, generated)

expected = list(fixture_doc["dry_run_expected_outputs"])
if ARGV.include?("--print")
  puts YAML.dump("dry_run_expected_outputs" => generated)
  exit 0
end

if generated == expected
  puts "Semantic compiler dry-run generator OK"
  puts "Generated outputs: #{generated.size}"
  puts "Compiler outcomes: #{fixtures.map { |fixture| fixture["compiler_outcome"] }.compact.uniq.sort.join(", ")}"
  exit 0
end

warn "Generated dry-run output differs from fixture dry_run_expected_outputs."
warn JSON.pretty_generate("expected" => expected, "generated" => generated)
exit 1
