"""`python -m lifein.evals <子命令>`。

    python -m lifein.evals planner                       # 跑一个 agent 的评测集
    python -m lifein.evals all                           # 全部
    python -m lifein.evals export --user <uuid> --agent planner   # 攒负样本

**跑评测会真的调外部模型、真的花钱**(06 §7.4),所以它是手动动作:
不进 CI、不进 pytest。改完 prompt 跑一次,看通过率有没有掉。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from lifein.agents.contract import get_agent, registered_agents
from lifein.bootstrap import register_tools
from lifein.config import get_settings
from lifein.db import session_scope
from lifein.evals import dump_case
from lifein.evals.export import export_memory_negatives, export_planner_negatives
from lifein.evals.runner import run_agent
from lifein.llm.client import LLMClient

log = logging.getLogger("lifein.evals")


def _llm() -> LLMClient:
    s = get_settings()
    return LLMClient(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key.get_secret_value(),
        model=s.llm_model,
        timeout_s=s.llm_timeout_s,
        max_retries=s.llm_max_retries,
        price_prompt_per_1k=Decimal(str(s.llm_price_prompt_per_1k)),
        price_completion_per_1k=Decimal(str(s.llm_price_completion_per_1k)),
        embedding_model=s.embedding_model,
        embedding_dim=s.embedding_dim,
    )


def cmd_run(args: argparse.Namespace) -> int:
    register_tools()  # agent 注册表要先有东西,评测才找得到契约
    names = sorted(registered_agents()) if args.agent == "all" else [args.agent]

    failed = 0
    for name in names:
        spec = get_agent(name)
        if not Path(spec.evalset).exists():
            # 缺评测集要响亮地失败:ADR-013 说缺一项不许注册,
            # "文件不存在"该是同一种响亮
            print(f"# {name}:没有评测集({spec.evalset})", file=sys.stderr)
            failed += 1
            continue
        report = run_agent(name, llm=_llm())
        print(report.render())
        failed += report.total - report.passed

    return 1 if failed else 0


def cmd_export(args: argparse.Namespace) -> int:
    """从库里攒负样本。**只打印,不直接写进评测集** ——
    用户拒绝的原因不总是"agent 错了",合并之前要人看一遍(06 §7.3)。"""
    with session_scope() as session:
        if args.agent == "planner":
            cases = export_planner_negatives(
                args.user, session, now=datetime.now(UTC), limit=args.limit
            )
        elif args.agent == "memory":
            cases = export_memory_negatives(args.user, session, limit=args.limit)
        else:
            print(f"这个 agent 还没有负样本来源:{args.agent}", file=sys.stderr)
            return 1

    for case in cases:
        print(dump_case(case))

    print(
        f"\n# 上面 {len(cases)} 条是候选,不是结论。"
        "逐条看过再追加进 evals/ —— 用户拒绝过的东西里,"
        "有一部分只是他自己记得,那种不该进负样本。",
        file=sys.stderr,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lifein.evals")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="跑评测集(会调模型、会花钱)")
    run.add_argument("agent", help="agent 名,或 all")
    run.set_defaults(func=cmd_run)

    export = sub.add_parser("export", help="从库里攒负样本")
    export.add_argument("--user", required=True)
    export.add_argument("--agent", required=True, choices=("planner", "memory"))
    export.add_argument("--limit", type=int, default=50)
    export.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    raw = list(sys.argv[1:] if argv is None else argv)
    # `python -m lifein.evals planner` 是最常打的那一条,让它不用写 run
    if raw and raw[0] not in {"run", "export", "-h", "--help"}:
        raw = ["run", *raw]
    args = build_parser().parse_args(raw)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
