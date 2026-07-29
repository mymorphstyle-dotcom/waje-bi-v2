# vNext contracts

Gate 1 起，本目录承载版本化 domain、API、event 与 semantic schemas。Python
领域对象位于 `services/analysis_core/src/waje_vnext/domain`；TypeScript
绑定由同一批 JSON Schema 生成到 `contracts/generated/typescript`。

生成与漂移检查：

```bash
npm run generate:contracts
npm run check:contracts
```

Gate 2 在当前合同上加入 model-native proposal、完整 authority projection 的
`ContextPacket` 和 `ControllerState`。当前目录不读取历史 contract runtime，也不维护旧
action payload 的兼容分支。
