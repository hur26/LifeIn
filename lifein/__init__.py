"""LifeIn 服务端。

分层见 docs/02-architecture.md §1,包结构与分层一一对应:

    api/         接入层  —— 企微回调、采集上报、App 查询
    scheduler    触发层  —— 定时调度与数据源轮询
    agents/      编排层  —— 按领域分工的 agent,同进程运行
    governance/  治理层  —— 工具注册表、分权网关、审批、审计
    tools/       能力层  —— 工具实现
    sources/     能力层  —— 数据源适配器
    channels/    接入层  —— 推送通道(企微 / 邮件)
    models/      记忆层  —— 归一化骨架与 ORM 映射

治理层是强制通道:能力层的工具不允许被编排层直接调用。
这条在代码结构上强制 —— agents/ 不许 import tools/。
"""

__version__ = "0.0.1"
