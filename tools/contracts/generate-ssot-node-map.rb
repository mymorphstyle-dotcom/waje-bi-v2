#!/usr/bin/env ruby
# frozen_string_literal: true

require "rexml/document"
require "yaml"

ROOT = File.expand_path("../..", __dir__)
SSOT = File.join(ROOT, "contracts/ssot/付费金额影响因子分析.mm")
OUT = File.join(ROOT, "contracts/ledger/ssot-node-reconciliation.yaml")

GROUP_RULES = [
  ["gameplay_ggr_and_betting", /返奖率|GGR|玩法流水|下注/],
  ["gameplay_engagement_and_arpu", /玩法icon|玩法付费|玩法ARPU|玩法arpu|玩法人数|玩法活跃|首页推荐|banner|push|位置|icon的UI设计|三方游戏/],
  ["amount_tier_and_user_value", /充值档位|单笔付费金额|客单|大额支付|分层用户|充值引导|充值活动|单笔充值金额/],
  ["payment_status_latency_and_quality", /支付成功率|发起支付|支付次数|支付环境|支付功能|支付流程|银行稳定性|三方支付|grafana|支付渠道/],
  ["payment_channel_and_method", /支付渠道|三方支付通道|银行稳定性/],
  ["geo_device_environment", /地理|机型|手机型号|设备|网络波动|电力波动|极端天气/],
  ["external_context_events", /竞品|点点|外部环境|重大赛事|社会事件|政策|法案|媒体政策|体育新闻|官网|官方媒体|天气|电力波动|网络波动/],
  ["marketing_channel_and_growth_ops", /预算|出价|素材|SEO|GEO|用户推荐|外部联运|投放|campaign|CTR|CVR|关键词/],
  ["product_operation_events", /产品|服务器|注册流程|注册页|注册激励|首充礼包|新手引导|手动记录|日志|运营|活动/],
  ["user_acquisition_and_first_payment", /新增|注册率|首充|首次付费|留存|付费日活/],
  ["payment_order_metric_chain", /付费频次|付费人数|付费转化率|付费次数|ARPPU|付费日活arpu/],
  ["calendar_time_and_payday", /时间分布|每小时|发薪|节假日/],
  ["paid_amount_metric_source", /付费金额|公式|sum/]
].freeze

def factor_group(label, path)
  text = ([label] + path).join(" / ")
  GROUP_RULES.each { |group, pattern| return group if text.match?(pattern) }
  "paid_amount_metric_source"
end

def scope_status(group, label)
  return "out_of_scope_for_now" if label.match?(/抓取|论坛|新闻网站/)
  return "missing_contract" if label.match?(/预算|出价|campaign|CTR|CVR|SEO|GEO|用户推荐|服务器|Grafana|日志|产品更新|首充礼包|充值活动|返奖率|玩法付费|玩法icon|支付订单到玩法/)
  return "missing_contract" if label.match?(/IP|设备ID/)

  {
    "paid_amount_metric_source" => "contract_backed",
    "payment_order_metric_chain" => "evidence_linked",
    "user_acquisition_and_first_payment" => "evidence_linked",
    "calendar_time_and_payday" => "static_assumption",
    "external_context_events" => "evidence_linked",
    "gameplay_ggr_and_betting" => "evidence_linked",
    "geo_device_environment" => label.match?(/电力波动|极端天气/) ? "evidence_linked" : "contract_backed"
  }.fetch(group, "missing_contract")
end

rows = []
doc = REXML::Document.new(File.read(SSOT))

walk = lambda do |node, path, depth|
  id = node.attributes["ID"]
  label = node.attributes["TEXT"].to_s.strip
  group = factor_group(label, path)
  rows << {
    "node_id" => id,
    "label" => label,
    "depth" => depth,
    "factor_group_id" => group,
    "data_contract_state" => scope_status(group, label),
    "review_status" => "mapped_by_rule_pending_owner_review"
  }
  node.elements.each("node") { |child| walk.call(child, path + [label], depth + 1) }
end

walk.call(doc.root.elements["node"], [], 0)

payload = {
  "contract_version" => "0.1",
  "artifact" => "ssot_node_reconciliation",
  "review_status" => "generated_pending_owner_review",
  "source_refs" => {
    "ssot_file" => "contracts/ssot/付费金额影响因子分析.mm",
    "factor_ledger" => "contracts/ledger/factor-ledger.yaml"
  },
  "generation" => {
    "tool" => "tools/contracts/generate-ssot-node-map.rb",
    "node_count" => rows.size,
    "rule_note" => "Keyword map for review coverage; owner review can override factor_group_id and data_contract_state."
  },
  "nodes" => rows
}

File.write(OUT, "#{payload.to_yaml(line_width: -1)}\n")
puts "Wrote #{OUT} with #{rows.size} nodes."
