# Gate 3 DeepSeek 三角色配置质量探针

## 结论

本轮仅用于选择待校准配置，不能替代 Gate 3 的独立人工校准。

暂定配置：

| 角色 | 配置 | 选择依据 |
| --- | --- | --- |
| Primary Business Analysis Agent | `deepseek-v4-pro`，thinking enabled | 五个开放业务问题的盲评平均排名 `1.133`，三项平均分 `2.933/3`，四组中最高 |
| Runtime Reviewer | `deepseek-v4-pro`，thinking disabled | 六个发布/修复目标案例中，publication decision 与 repair target 均为 `100%`，延迟 `8.896s` |
| Evaluation Reviewer | `deepseek-v4-flash`，thinking enabled | 六个隐藏 verdict 案例中，verdict accuracy、valid-design acceptance、failure recall 均为 `100%`；与另两角色配置保持独立 |

三个 profile 当前 lifecycle 均为 `quality_probe_only`。只有经过 hash-bound 正式样本、独立人工标签和 Gate 3 calibration policy 后，才能转为正式 Reviewer 配置。

## 方法

探针脚本：

- `vnext/tools/probe_gate3_deepseek_role_profiles.py`

原始结果：

- `artifacts/gate3/deepseek-role-profile-probe/20260730T202421Z.json`

比较四组配置：

1. `deepseek-v4-flash`，thinking disabled；
2. `deepseek-v4-flash`，thinking enabled；
3. `deepseek-v4-pro`，thinking disabled；
4. `deepseek-v4-pro`，thinking enabled。

Primary 使用五个开放业务案例，覆盖跨月比较、付费变化解释、事件影响、数据不完整和用户纠正。四组输出由四组配置盲评，候选标签随机化，候选自己的评分从汇总中排除。

Runtime Reviewer 与 Evaluation Reviewer 使用六个隐藏答案案例，覆盖：

- 两种合理日期设计；
- 同月漂移；
- 不等 exposure 下使用 raw total；
- 答案方向与证据相反；
- 缺失日期下的 provisional answer。

## 结果

### Primary

| 配置 | 平均排名，越低越好 | 平均维度分 | 生成延迟 |
| --- | ---: | ---: | ---: |
| Flash，无 thinking | 2.533 | 2.111 | 18.990s |
| Flash，有 thinking | 4.000 | 1.511 | 21.966s |
| Pro，无 thinking | 2.000 | 2.356 | 31.267s |
| Pro，有 thinking | 1.133 | 2.933 | 103.619s |

Pro thinking 在开放测量设计、替代解释、revision/fencing 和证据边界上明显领先。它的代价是时延最高，因此只用于持续拥有开放业务语义的 Primary。

### Runtime Reviewer

| 配置 | 发布决策准确率 | 修复目标准确率 | 延迟 |
| --- | ---: | ---: | ---: |
| Flash，无 thinking | 83.33% | 100% | 5.781s |
| Flash，有 thinking | 100% | 100% | 18.658s |
| Pro，无 thinking | 100% | 100% | 8.896s |
| Pro，有 thinking | 100% | 100% | 72.955s |

Pro 无 thinking 在这组探针里同时满足准确率和时延要求。它能正确把日期/ exposure 漂移修回 Frame，把文字方向错误修回 Answer，并接受两种有依据的日期设计。

### Evaluation Reviewer

| 配置 | verdict 准确率 | 合理设计接受率 | 错误召回率 | 延迟 |
| --- | ---: | ---: | ---: | ---: |
| Flash，无 thinking | 83.33% | 66.67% | 100% | 7.338s |
| Flash，有 thinking | 100% | 100% | 100% | 25.538s |
| Pro，无 thinking | 100% | 100% | 100% | 7.517s |
| Pro，有 thinking | 100% | 100% | 100% | 47.767s |

Evaluation Reviewer 选择 Flash thinking，兼顾当前探针质量和三角色配置独立性。Pro 无 thinking 的表现也达到满分，保留为后续 challenger。

## 防止探针被误当正式校准

正式 Gate 3 calibration 额外要求：

- 至少 12 个 hash-bound Episode；
- 同时覆盖 critical/non-critical、base/counterfactual、pass/fail/blocked；
- 至少 4 个人工 non-pass 标签；
- grader 与人工一致率至少 80%；
- critical false pass 为 0；
- calibration reviewer 与 Episode 的 business owner、measurement reviewer 相互独立；
- prompt、rubric、input/output schema、runner 和模型配置全部冻结并纳入 authority hash。

因此，本探针只能说明“这三组配置值得进入正式校准”，不能说明 Reviewer 已具备上线资格。
