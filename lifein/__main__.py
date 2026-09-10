"""进程入口:`python -m lifein`。

一个进程既收企微回调又跑定时任务(ADR-016 的"同进程")。
默认只监听 127.0.0.1,公网访问走反向代理 + TLS(07 §5 部署步骤)。

    python -m lifein            起服务
    python -m lifein --once     只跑一次摘要然后退出,用来实测链路
    python -m lifein --check    只做启动自检然后退出,一条消息都不发
"""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lifein")
    parser.add_argument(
        "--once",
        action="store_true",
        help="立刻跑一次每日摘要然后退出。部署后先用它验证链路,不用等到八点",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做启动自检(配置、库版本、装配)然后退出,不发任何消息、不改库",
    )
    args = parser.parse_args(argv)

    # 在 build_services 之前配好日志,否则配置校验失败的那条信息会看不见
    from lifein.config import get_settings

    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("lifein")

    if args.check:
        return _check(settings, log)

    # **在装配之前先比一次库版本**(ADR-030)。2026-09-10 那次事故里,库停在
    # 0007、代码在 0014,而进程照常启动 —— 缺的列直到 job 跑起来才被读到,
    # 于是"部署漏了一条命令"以"审批执行异常"的形状每三分钟报一次。
    #
    # 连不上库不在这里拦:那是另一回事,而且 run-lifein.cmd / systemd 本来
    # 就会重试。这里只拦"连上了,但对不上"。
    from lifein.db import get_engine
    from lifein.schema_guard import SchemaOutOfDate, ensure_schema_current

    try:
        ensure_schema_current(get_engine(settings), auto_upgrade=settings.schema_auto_upgrade)
    except SchemaOutOfDate as exc:
        log.error("库版本对不上,不启动:%s", exc)
        return 2
    except Exception:  # noqa: BLE001 —— 连不上库只是"这次没查成",不该挡住启动
        log.exception("库版本没校验成,继续启动")

    from lifein.bootstrap import build_services

    services = build_services(settings)

    if args.once:
        from lifein.scheduler import run_digest_for_all_users

        pushed = run_digest_for_all_users(services)
        log.info("摘要跑完,推送 %d 人", pushed)
        return 0

    import uvicorn

    from lifein.api.app import create_app

    uvicorn.run(
        create_app(services, with_scheduler=True),
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )
    return 0


def _check(settings, log: logging.Logger) -> int:
    """启动自检:配置、库版本、装配。**一条消息都不发,一个字都不改。**

    `scripts/restore-drill.md` 第五步要的就是它:还原出来的那个库能不能被
    这份代码正常启动 —— 而**真让它跑起来会往真实通道发消息**,演练不该打扰
    任何人。`migrations/env.py` 的注释里也提到它。

    **它不自动升级,哪怕 `SCHEMA_AUTO_UPGRADE` 开着。** 自检的语义是"告诉我
    现在是什么样",而演练库和备份还原出来的库尤其不该被顺手改掉
    ([ADR-030](../docs/04-tech-decisions.md))。
    """
    ok = True

    # 配置:走到这里就说明 get_settings() 已经过了
    log.info("配置校验通过(时区 %s,监听 %s:%s)", settings.tz, settings.app_host, settings.app_port)

    from lifein.db import get_engine
    from lifein.schema_guard import inspect_schema

    try:
        state = inspect_schema(get_engine(settings))
    except Exception as exc:  # noqa: BLE001
        log.error("库连不上或版本读不出来:%s: %s", type(exc).__name__, exc)
        return 1

    if state.is_current:
        log.info("库版本:%s", state.describe())
    else:
        ok = False
        hint = "" if state.unknown else " —— 跑 `python -m alembic upgrade head`"
        log.error("库版本:%s%s", state.describe(), hint)

    # 装配:工具注册、agent 白名单、通道与告警出口拼不拼得起来。
    # 拼得起来不等于通道是通的 —— 那要 `admin test-imap` / `admin test-alert`
    try:
        from lifein.bootstrap import build_services

        services = build_services(settings)
    except Exception as exc:  # noqa: BLE001
        log.error("装配失败:%s: %s", type(exc).__name__, exc)
        return 1

    log.info(
        "装配通过(告警出口 %s,推送 %s)",
        type(services.alerter).__name__,
        type(services.channel).__name__,
    )
    log.info("自检%s", "通过" if ok else "没过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
