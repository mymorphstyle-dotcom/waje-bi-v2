#!/usr/bin/env ruby
# frozen_string_literal: true

require "date"
require "fileutils"
require "open3"
require "optparse"
require "rbconfig"
require "yaml"

ROOT = File.expand_path("../..", __dir__)
HARNESS_PATH = File.join(ROOT, "tools/evals/run-semantic-compiler-contract-harness.rb")
DEFAULT_OUT = File.join(ROOT, "data/local/semantic-compiler-dry-run-artifacts")

def load_yaml(path)
  YAML.safe_load(File.read(path), permitted_classes: [Date], aliases: true) || {}
end

def list(value)
  value.is_a?(Array) ? value : []
end

def run_harness(env = {})
  output, status = Open3.capture2e(env, RbConfig.ruby, HARNESS_PATH, "--print")
  raise "contract harness failed:\n#{output}" unless status.success?

  load_yaml_from_string(output).fetch("semantic_compiler_contract_bundles")
end

def load_yaml_from_string(text)
  YAML.safe_load(text, permitted_classes: [Date], aliases: true) || {}
end

def bundles_from_input(path)
  return run_harness if path.nil?

  doc = load_yaml(path)
  return list(doc["semantic_compiler_contract_bundles"]) if doc["semantic_compiler_contract_bundles"]
  return run_harness("WAJE_SEMANTIC_COMPILER_FIXTURES" => File.expand_path(path)) if doc["fixtures"]

  raise "#{path}: expected semantic_compiler_contract_bundles or fixtures"
end

def artifact_for(bundle)
  summary = bundle["validation_summary"] || {}
  fixture_id = bundle.fetch("fixture_id")

  {
    "run_id" => "dry_run_#{fixture_id.downcase.tr("-", "_")}",
    "fixture_id" => fixture_id,
    "question_family" => bundle.fetch("question_family"),
    "compiler_outcome" => bundle.fetch("compiler_outcome"),
    "accepted_graph_input" => bundle.fetch("accepted_graph_input"),
    "semantic_query_request" => list(bundle["semantic_query_request"]),
    "semantic_query_response_skeleton" => list(bundle["semantic_query_response_skeleton"]),
    "evidence_envelopes" => list(bundle["evidence_envelopes"]),
    "answer_package_handoff" => bundle.fetch("answer_package_handoff"),
    "path_records" => list(bundle["path_records"]),
    "validation_summary" => summary,
    "contract_refs" => {
      "contract_pins" => list(bundle.dig("accepted_graph_input", "contract_pins")),
      "support_ids" => list(summary["support_ids"]),
      "backlog_refs" => list(summary["backlog_refs"]),
      "limitation_refs" => list(summary["limitation_refs"])
    },
    "non_runtime_notice" => bundle.fetch("non_runtime_notice")
  }
end

options = { out: DEFAULT_OUT, print: false, input: nil }
OptionParser.new do |parser|
  parser.banner = "Usage: ruby tools/runtime/run-semantic-compiler-dry-run-artifact.rb [--input PATH] [--out DIR] [--print]"
  parser.on("--input PATH", "Fixture YAML or harness bundle YAML") { |value| options[:input] = value }
  parser.on("--out DIR", "Artifact output directory") { |value| options[:out] = File.expand_path(value) }
  parser.on("--print", "Print artifact bundle YAML to stdout") { options[:print] = true }
end.parse!

artifacts = bundles_from_input(options[:input]).map { |bundle| artifact_for(bundle) }
raise "expected 8 artifacts, generated #{artifacts.size}" unless artifacts.size == 8

if options[:print]
  puts YAML.dump("semantic_compiler_dry_run_artifacts" => artifacts)
  exit 0
end

FileUtils.mkdir_p(options[:out])
artifacts.each do |artifact|
  path = File.join(options[:out], "#{artifact.fetch("fixture_id")}.artifact.yaml")
  File.write(path, YAML.dump(artifact))
end

puts "Semantic compiler dry-run artifacts written."
puts "Artifacts: #{artifacts.size}"
puts "Output: #{options[:out]}"
