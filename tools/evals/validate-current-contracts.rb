#!/usr/bin/env ruby
# frozen_string_literal: true

require "open3"
require "rbconfig"

ROOT = File.expand_path("../..", __dir__)

COMMANDS = [
  [RbConfig.ruby, "tools/evals/validate-launch-evals.rb"],
  [RbConfig.ruby, "tools/contracts/validate-contracts.rb"],
  [RbConfig.ruby, "tools/runtime/load-contracts-to-postgres.rb"],
  ["git", "diff", "--check"]
].freeze

COMMANDS.each do |command|
  puts "$ #{command.join(" ")}"
  output, status = Open3.capture2e(*command, chdir: ROOT)
  puts output unless output.empty?
  next if status.success?

  warn "Current contract validation failed at: #{command.join(" ")}"
  exit status.exitstatus || 1
end

puts "Current contract validation passed."
