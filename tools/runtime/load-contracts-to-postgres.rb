#!/usr/bin/env ruby
# frozen_string_literal: true

require "digest"
require "fileutils"
require "open3"

ROOT = File.expand_path("../..", __dir__)
CONTAINER = ENV.fetch("WAJE_PG_CONTAINER", "waje-bi-postgres")
DB = ENV.fetch("WAJE_PG_DB", "waje_bi_runtime")
USER = ENV.fetch("WAJE_PG_USER", "waje")
OUT = File.join(ROOT, "data/local/runtime-mirror-load.sql")

def sh!(*cmd)
  stdout, stderr, status = Open3.capture3(*cmd)
  raise "#{cmd.join(" ")} failed:\n#{stdout}\n#{stderr}" unless status.success?

  stdout
end

def sql_string(value)
  "'#{value.gsub("'", "''")}'"
end

paths = Dir.glob(File.join(ROOT, "contracts/**/*.yaml")).sort
FileUtils.mkdir_p(File.dirname(OUT))

sql = +"BEGIN;\n"
schema_path = File.join(ROOT, "tools/runtime/postgres-runtime-mirror.sql")
sql << File.read(schema_path)
sql << "\nTRUNCATE waje_runtime.active_contracts;\n"

paths.each do |path|
  rel = path.delete_prefix("#{ROOT}/")
  text = File.read(path)
  tag = "yaml_#{Digest::SHA256.hexdigest(rel)[0, 12]}"
  sha = Digest::SHA256.hexdigest(text)
  sql << <<~SQL
    INSERT INTO waje_runtime.contract_artifacts(path, sha256, yaml_text, mirrored_at)
    VALUES (#{sql_string(rel)}, #{sql_string(sha)}, $#{tag}$#{text}$#{tag}$, now())
    ON CONFLICT (path) DO UPDATE
    SET sha256 = EXCLUDED.sha256, yaml_text = EXCLUDED.yaml_text, mirrored_at = now();
    INSERT INTO waje_runtime.active_contracts(path)
    VALUES (#{sql_string(rel)})
    ON CONFLICT (path) DO UPDATE SET activated_at = now();
  SQL
end

sql << "INSERT INTO waje_runtime.mirror_loads(artifact_count, note) VALUES (#{paths.size}, 'contracts yaml mirror');\n"
sql << "COMMIT;\n"
File.write(OUT, sql)

target = "/tmp/waje-runtime-mirror-load.sql"
sh!("docker", "cp", OUT, "#{CONTAINER}:#{target}")
puts sh!("docker", "exec", CONTAINER, "psql", "-U", USER, "-d", DB, "-v", "ON_ERROR_STOP=1", "-f", target)
puts "Mirrored #{paths.size} contract YAML files into #{CONTAINER}/#{DB}."
