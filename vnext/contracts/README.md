# vNext contracts

Gate 1 起，本目录承载版本化 domain、API、event 与 semantic schemas。Python
领域对象位于 `services/analysis_core/src/waje_vnext/domain`；TypeScript
绑定由同一批 JSON Schema 生成到 `contracts/generated/typescript`。

authority、typed action 与 ContextPacket 已切换到 schema epoch 3：
`authority.v3`、`actions.v3`、`context-packet.v3`。epoch 1 对应文件已删除；当前开发期
不提供旧 Frame 自由文本合同的兼容入口。

生成与漂移检查：

```bash
npm run generate:contracts
npm run check:contracts
```

Gate 2 在当前合同上加入 model-native proposal、完整 authority projection 的
`ContextPacket` 和 `ControllerState`。当前目录不读取历史 contract runtime，也不维护旧
action payload 的兼容分支。

Gate 3 G3.5 当前合同新增：

- `evidence.v1`：CapabilityResultEnvelope、tagged execution provenance、EvidenceRecord、
  admission、validity、use binding 与 obligation satisfaction；
- `answering.v1`：LLM-owned claim proposal、system-owned claim/precheck、provisional
  Answer 与 settlement precondition；
- `workflow.v1`：四轴 Workflow snapshot、application receipt 与 projection head。

生成代码只覆盖当前合同。旧 Evidence/Answer placeholder、caller-owned claim ID 与自由文本
applicability 已删除，没有兼容 decoder。
