#!/usr/bin/env ruby
# frozen_string_literal: true

require "open3"
require "rbconfig"

ROOT = File.expand_path("../..", __dir__)

COMMANDS = [
  [RbConfig.ruby, "tools/evals/generate-semantic-compiler-dry-run.rb"],
  [RbConfig.ruby, "tools/evals/run-semantic-compiler-contract-harness.rb"],
  [RbConfig.ruby, "tools/runtime/run-semantic-compiler-dry-run-artifact.rb"],
  [RbConfig.ruby, "tools/runtime/validate-semantic-compiler-dry-run-artifacts.rb"],
  [RbConfig.ruby, "tools/evals/validate-semantic-compiler-dry-run.rb"],
  [RbConfig.ruby, "tools/evals/validate-semantic-compiler-fixtures.rb"],
  [RbConfig.ruby, "tools/evals/validate-launch-evals.rb"],
  [RbConfig.ruby, "tools/contracts/validate-contracts.rb"],
  [RbConfig.ruby, "tools/runtime/load-contracts-to-postgres.rb"],
  ["git", "diff", "--check"]
].freeze

COMMANDS.each do |cmd|
  puts "$ #{cmd.join(" ")}"
  output, status = Open3.capture2e(*cmd, chdir: ROOT)
  puts output unless output.empty?
  unless status.success?
    warn "Phase 3 validation failed at: #{cmd.join(" ")}"
    exit status.exitstatus || 1
  end
end

puts "Phase 3 validation passed."
