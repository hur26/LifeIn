# 评测集

格式与跑法见 [06 §7](../docs/06-data-model.md#7-评测集格式)。

**这个目录里的 `*.jsonl` 不进版本库**(`.gitignore` 里排除了),
因为样本是你真实的邮件与群消息 —— 它们和
[R12](../docs/05-risks.md#r12--外部-llm-供应商侧的数据暴露) 同级敏感。
进库的只有 `*.example.jsonl`:形状给人看,内容是编的。

开工方式:

```bash
cp evals/planner.example.jsonl evals/planner.jsonl   # 拿示例当起点
python -m lifein.evals export --user <uuid> --agent planner >> evals/planner.jsonl
python -m lifein.evals planner
```

**导出来的负样本要逐条看过再留下。** 用户拒绝一条待确认的原因不总是
"agent 错了",也可能是"这事我自己记得" —— 后一种不该进负样本。

每个 agent 至少 20 条(ADR-013)。**攒不到不是拖延,是这个 agent 还没被
真正用过** —— 那时先去用它,不要编样本凑数。
