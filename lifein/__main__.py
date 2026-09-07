"""进程入口:`python -m lifein`。

一个进程既收企微回调又跑定时任务(ADR-016 的"同进程")。
默认只监听 127.0.0.1,公网访问走反向代理 + TLS(07 §5)。

    python -m lifein            起服务
    python -m lifein --once     只跑一次摘要然后退出,用来实测链路
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
    args = parser.parse_args(argv)

    # 在 build_services 之前配好日志,否则配置校验失败的那条信息会看不见
    from lifein.config import get_settings

    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from lifein.bootstrap import build_services

    services = build_services(settings)

    if args.once:
        from lifein.scheduler import run_digest_for_all_users

        pushed = run_digest_for_all_users(services)
        logging.getLogger("lifein").info("摘要跑完,推送 %d 人", pushed)
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


if __name__ == "__main__":
    sys.exit(main())
