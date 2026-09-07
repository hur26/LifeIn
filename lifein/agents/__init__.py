"""编排层:按领域分工的 agent。

每个 agent 必须声明五项契约:输入类型、工具白名单、输出 schema、失败行为、评测集。
缺一项不许注册 —— 见 docs/04-tech-decisions.md ADR-013。
"""
