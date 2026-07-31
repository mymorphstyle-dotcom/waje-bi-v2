# vNext evals

eval 使用真实用户措辞与结构化 expectation package，覆盖真实问题、历史失败模式和矩阵
生成边界案例。旧断言不进入本目录。

Gate 3 从 [`gate3/README.md`](gate3/README.md) 定义的行为优先 `EvaluationEpisode` 开始。
Episode 独立于 runtime 类、action、工具顺序和 SQL 形状，先验证业务决策、测量质量、
动态调查与证据边界。authority/trust conformance 和局部 implementation tests 作为另外两层
验收，后两层无法抵消业务 Episode 失败。
