"""Alembic 运行环境。

**刻意不加载完整的 Settings。** 跑一次迁移不该要求先配好企微密钥和 LLM key ——
迁移只需要一个 DATABASE_URL。启动期校验的严格是给服务进程的,不是给运维命令的。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import engine_from_config, pool


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 只在没人设过的时候才从 DATABASE_URL 取。
# 无条件覆盖会把程序化传入的 url 吃掉 —— 测试夹具要指向另一个库,
# 而"配置被静默忽略"是那种能耗掉一小时才想明白的问题。
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option(
        "sqlalchemy.url",
        MigrationSettings().database_url,  # type: ignore[call-arg]
    )

# 不设 target_metadata:本项目的 DDL 由迁移脚本手写,不用 autogenerate。
# 原因见 lifein/db.py —— 06 里那几条 CHECK 约束是安全机制,
# autogenerate 表达不了,而它一旦生成"差异"就会把它们删掉。
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
