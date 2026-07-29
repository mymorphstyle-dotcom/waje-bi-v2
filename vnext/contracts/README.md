# vNext contracts

Gate 1 起，本目录承载版本化 domain、API、event 与 semantic schemas。Python
领域对象位于 `services/analysis_core/src/waje_vnext/domain`；TypeScript
绑定由同一批 JSON Schema 生成到 `contracts/generated/typescript`。

生成与漂移检查：

```bash
npm run generate:contracts
npm run check:contracts
```

当前目录不读取历史 contract runtime。
