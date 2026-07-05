#!/usr/bin/env ruby
# frozen_string_literal: true

require "date"
require "optparse"
require "set"
require "yaml"

ROOT = File.expand_path("../..", __dir__)
DEFAULT_DIR = File.join(ROOT, "data/local/semantic-compiler-dry-run-artifacts")
FIXTURE_PATH = File.join(ROOT, "evals/semantic-compiler/semantic-compiler-fixtures.yaml")

REQUIRED_FIELDS = %w[
  run_id
  fixture_id
  question_family
  compiler_outcome
  accepted_graph_input
  semantic_query_request
  semantic_query_response_skeleton
  evidence_envelopes
  answer_package_handoff
  path_records
  validation_summary
  contract_refs
  non_runtime_notice
].freeze
REQUIRED_NOTICE_PHRASES = ["not runtime output", "no sql", "no database", "no capability execution", "no real business conclusion"].freeze
SQL_PATTERNS = [
  /\bselect\b.+\bfrom\b/i,
  /\binsert\s+into\b/i,
  /\bupdate\b.+\bset\b/i,
  /\bdelete\s+from\b/i,
  /\bcreate\s+(table|view|schema)\b/i,
  /\bdrop\s+(table|view|schema)\b/i
].freeze
FORBIDDEN_KEYS = %w[
  raw_sql
  sql
  sql_text
  compiled_sql
  executed_sql
  final_answer
  answer_text
  business_conclusion
  real_business_conclusion
  published_answer_package
  raw_user_ids
  raw_ip_values
  raw_device_ids
  user_id_values
  ip_values
  device_id_values
  typed_payload_values
].to_set.freeze

def list(value)
  value.is_a?(Array) ? value : []
end

def blank?(value)
  value.nil? || (value.respond_to?(:empty?) && value.empty?)
end

def load_yaml(path)
  YAML.safe_load(File.read(path), permitted_classes: [Date], aliases: true) || {}
end

def walk(value, path = [], &block)
  yield(path, value)
  case value
  when Hash
    value.each { |key, child| walk(child, path + [key.to_s], &block) }
  when Array
    value.each_with_index { |child, index| walk(child, path + [index.to_s], &block) }
  end
end

def evidence_refs_from_handoff(handoff)
  list(handoff["claim_group_bindings"]).flat_map { |binding| list(binding["required_evidence_refs"]) }.compact.to_set
end

def refs_from_artifact(artifact, key)
  path_refs = list(artifact["path_records"]).flat_map { |path| list(path[key]) }
  envelope_refs = list(artifact["evidence_envelopes"]).flat_map do |envelope|
    list(envelope["limitations"]).flat_map { |limitation| list(limitation[key]) }
  end
  summary_refs = list(artifact.dig("contract_refs", key))
  (path_refs + envelope_refs + summary_refs).compact
end

def validate_no_raw_output!(errors, artifact)
  owner = "artifact #{artifact["fixture_id"] || "(missing fixture_id)"}"
  walk(artifact) do |path, value|
    key = path.last.to_s
    errors << "#{owner}: forbidden key #{path.join(".")}" if FORBIDDEN_KEYS.include?(key)

    next unless value.is_a?(String)

    SQL_PATTERNS.each do |pattern|
      errors << "#{owner}: raw SQL-looking text at #{path.join(".")}" if value.match?(pattern)
    end
    errors << "#{owner}: raw IPv4-looking value at #{path.join(".")}" if value.match?(/\b(?:\d{1,3}\.){3}\d{1,3}\b/)
  end
end

options = { dir: DEFAULT_DIR }
OptionParser.new do |parser|
  parser.banner = "Usage: ruby tools/runtime/validate-semantic-compiler-dry-run-artifacts.rb [--dir DIR]"
  parser.on("--dir DIR", "Artifact directory") { |value| options[:dir] = File.expand_path(value) }
end.parse!

errors = []
fixture_doc = load_yaml(FIXTURE_PATH)
required_fixture_ids = list(fixture_doc["fixtures"]).map { |fixture| fixture["fixture_id"] }.compact.to_set
support_doc = load_yaml(File.join(ROOT, "contracts/ledger/capability-support.yaml"))
backlog_doc = load_yaml(File.join(ROOT, "contracts/backlog/missing-contracts.yaml"))
factor_doc = load_yaml(File.join(ROOT, "contracts/ledger/factor-ledger.yaml"))

support_ids = list(support_doc["support_records"]).map { |record| record["support_id"] }.compact.to_set
backlog_ids = list(backlog_doc["backlog"]).map { |record| record["backlog_id"] }.compact.to_set
limitation_ids = list(factor_doc["review_limitations"]).map { |record| record["limitation_id"] }.compact.to_set

files = Dir.glob(File.join(options[:dir], "*.{yaml,yml,json}")).sort
artifacts = files.map do |path|
  load_yaml(path).merge("__path" => path)
rescue Psych::Exception => e
  errors << "#{path}: parse failed: #{e.message}"
  nil
end.compact

artifact_by_fixture = artifacts.to_h { |artifact| [artifact["fixture_id"], artifact] }
missing = required_fixture_ids - artifact_by_fixture.keys.to_set
extra = artifact_by_fixture.keys.compact.to_set - required_fixture_ids
errors << "missing artifacts for fixtures: #{missing.to_a.sort.join(", ")}" unless missing.empty?
errors << "unknown artifact fixture ids: #{extra.to_a.sort.join(", ")}" unless extra.empty?
errors << "expected 8 artifacts, found #{artifacts.size}" unless artifacts.size == 8

artifacts.each do |artifact|
  owner = "artifact #{artifact["fixture_id"] || "(missing fixture_id)"}"

  REQUIRED_FIELDS.each do |field|
    errors << "#{owner}: missing #{field}" unless artifact.key?(field) && !artifact[field].nil?
  end

  notice = artifact["non_runtime_notice"].to_s.downcase
  REQUIRED_NOTICE_PHRASES.each do |phrase|
    errors << "#{owner}: non_runtime_notice missing #{phrase.inspect}" unless notice.include?(phrase)
  end

  policies = list(artifact.dig("validation_summary", "query_execution_policy"))
  requests = list(artifact["semantic_query_request"])
  request_by_id = requests.to_h { |query| [query["semantic_query_id"], query] }
  envelopes = list(artifact["evidence_envelopes"])
  envelopes_by_query_id = envelopes.group_by { |envelope| envelope["semantic_query_id"] }

  if artifact["compiler_outcome"] == "block" && policies.none? { |policy| policy["executable_query_request"] == false }
    errors << "#{owner}: blocked compiler outcome has no non-executable query"
  end

  policies.each do |policy|
    query = request_by_id[policy["semantic_query_id"]]
    if policy["runtime_status"] == "blocked"
      errors << "#{owner}/#{policy["semantic_query_id"]}: blocked policy is executable" unless policy["executable_query_request"] == false
      errors << "#{owner}/#{policy["semantic_query_id"]}: missing block_reason" if blank?(policy["block_reason"])
    end

    next unless policy["runtime_status"] == "degraded"

    query_path_refs = list(query && query["disabled_degraded_blocked_path_refs"])
    query_wording = query&.dig("expected_evidence_contract", "wording_limit")
    envelope_has_limit = list(envelopes_by_query_id[policy["semantic_query_id"]]).any? do |envelope|
      !blank?(envelope["wording_limit"]) && !list(envelope["limitations"]).empty?
    end
    if list(artifact["path_records"]).empty? || query_path_refs.empty? || blank?(query_wording) || !envelope_has_limit
      errors << "#{owner}/#{policy["semantic_query_id"]}: degraded query missing path records, limitations, or wording limit"
    end
  end

  evidence_refs = envelopes.map { |envelope| envelope["evidence_ref"] }.compact.to_set
  handoff_refs = evidence_refs_from_handoff(artifact["answer_package_handoff"] || {})
  missing_handoff_refs = evidence_refs - handoff_refs
  errors << "#{owner}: evidence refs missing from Answer Package handoff #{missing_handoff_refs.to_a.sort.join(", ")}" unless missing_handoff_refs.empty?

  list(artifact["semantic_query_response_skeleton"]).each do |response|
    list(response["evidence_handoff_refs"]).each do |ref|
      errors << "#{owner}/#{response["semantic_query_id"]}: response evidence ref #{ref.inspect} missing from Answer Package handoff" unless handoff_refs.include?(ref)
    end
  end

  artifact_support_ids = (
    list(artifact.dig("accepted_graph_input", "accepted_graph_nodes")).map { |node| node["support_id"] } +
    requests.map { |query| query["support_id"] } +
    envelopes.map { |envelope| envelope["support_id"] } +
    list(artifact.dig("contract_refs", "support_ids"))
  ).compact.uniq
  artifact_support_ids.each do |support_id|
    errors << "#{owner}: unknown support_id #{support_id.inspect}" unless support_ids.include?(support_id)
  end

  refs_from_artifact(artifact, "backlog_refs").uniq.each do |ref|
    errors << "#{owner}: unknown backlog_ref #{ref.inspect}" unless backlog_ids.include?(ref)
  end
  refs_from_artifact(artifact, "limitation_refs").uniq.each do |ref|
    errors << "#{owner}: unknown limitation_ref #{ref.inspect}" unless limitation_ids.include?(ref)
  end

  validate_no_raw_output!(errors, artifact)
end

if errors.empty?
  puts "Semantic compiler dry-run artifacts OK"
  puts "Artifacts: #{artifacts.size}"
  puts "Fixtures: #{artifact_by_fixture.keys.compact.sort.join(", ")}"
  exit 0
end

warn "Semantic compiler dry-run artifact validation failed:"
errors.each { |error| warn "- #{error}" }
exit 1
