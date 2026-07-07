#!/usr/bin/env ruby
# frozen_string_literal: true

require "open3"

ROOT = File.expand_path("../..", __dir__)
CONTAINER = ENV.fetch("WAJE_PG_CONTAINER", "waje-bi-postgres")
DB = ENV.fetch("WAJE_PG_DB", "waje_bi_runtime")
USER = ENV.fetch("WAJE_PG_USER", "waje")
SCHEMA = File.join(ROOT, "tools/runtime/conversation-runtime.sql")

def sh!(*cmd)
  stdout, stderr, status = Open3.capture3(*cmd)
  raise "#{cmd.join(" ")} failed:\n#{stdout}\n#{stderr}" unless status.success?

  stdout
end

target = "/tmp/waje-conversation-runtime.sql"
sh!("docker", "cp", SCHEMA, "#{CONTAINER}:#{target}")
puts sh!("docker", "exec", CONTAINER, "psql", "-U", USER, "-d", DB, "-v", "ON_ERROR_STOP=1", "-f", target)
puts "Loaded conversation runtime schema into #{CONTAINER}/#{DB}."
