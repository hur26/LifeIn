"""`.gitignore` 有没有误伤源码。不需要数据库,但需要 git。

**这一条是 2026-09 真踩过的:** 无前缀的 `data/` 规则匹配**每一层**目录,
于是 `android/app/src/main/java/ltd/iclab/lifein/data/` 整包被挡在仓库外 ——
里面是 `Secrets.kt`(Keystore)、`Enrollment.kt`(配码解析)、`Db.kt`(离线队列)。

**它的表现是最坏的那一种:本机编得过,clone 下来编不过。**
写代码的人看不到任何异常,而拿到仓库的人看到的是一串 `Unresolved reference`。
CI 也不会发现 —— 除非 CI 从干净的 clone 开始,而那正是这条测试替代的事。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SOURCE_SUFFIXES = {".py", ".kt", ".kts", ".sql", ".md", ".toml", ".cfg", ".ini"}
"""算作"源码"的后缀。**`.md` 在里面** —— 文档和代码在这个仓库里是同一等的东西
(AGENTS §4),一份被 ignore 掉的 ADR 和一个被 ignore 掉的类一样糟。"""

ALLOWED_IGNORED_DIRS = (
    ".venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    "android/.gradle/",
    "android/app/build/",
    "node_modules/",
)
"""这些底下的东西被忽略是对的:构建产物和依赖。"""


def ignored_paths() -> list[str]:
    """`git status --ignored` 列出来的所有被忽略的路径。"""
    result = subprocess.run(
        ["git", "status", "--ignored=matching", "--short"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"跑不了 git:{result.stderr.strip()}")
    return [
        line[3:].strip()
        for line in result.stdout.splitlines()
        if line.startswith("!!")
    ]


def test_no_source_file_is_ignored():
    """**被忽略的东西里不能有源码。**

    红了的时候不要急着把文件 `git add -f` —— 先看那条规则**为什么**匹配上了。
    十有八九是少了一个 `/` 前缀:`foo/` 匹配任意层级的 foo,
    `/foo/` 只匹配仓库根目录那一个。
    """
    offenders = []
    for path in ignored_paths():
        if path.startswith(ALLOWED_IGNORED_DIRS):
            continue
        target = ROOT / path
        if target.is_dir():
            offenders += [
                str(child.relative_to(ROOT))
                for child in target.rglob("*")
                if child.is_file() and child.suffix in SOURCE_SUFFIXES
            ]
        elif target.suffix in SOURCE_SUFFIXES:
            offenders.append(path)

    assert offenders == [], (
        "这些源码被 .gitignore 挡在仓库外了 —— **本机编得过,clone 下来编不过**:\n  "
        + "\n  ".join(offenders)
        + "\n少一个 `/` 前缀是最常见的原因(见 .gitignore 末尾那段)"
    )


def test_the_android_data_package_is_tracked():
    """**上面那条的具体化。** 泛化的检查在别人重构目录之后可能失效,
    而这三个文件是那次事故的当事人 —— 它们必须在仓库里,理由各自不同:

    - `Secrets.kt`:R11 要求长期凭据存 Keystore,这是那段代码
    - `Enrollment.kt`:配码解析。没有它 App 连不上服务端
    - `Db.kt`:上报队列与日历幂等表。没有它采集到的东西发不出去
    """
    result = subprocess.run(
        ["git", "ls-files", "android/app/src/main/java/ltd/iclab/lifein/data/"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    tracked = {Path(line).name for line in result.stdout.split()}
    assert {"Secrets.kt", "Enrollment.kt", "Db.kt"} <= tracked
