"""仓库里不许出现某一台开发机上的绝对路径。不需要数据库,但需要 git。

**这个项目在不止一台机器上开发。** 写死的路径在别的机器上不是"改一下就好":

- 它读起来像**要求**。下一个人会去建一个同名目录,而那个目录本该由
  `JAVA_HOME` / `ANDROID_HOME` / `local.properties` 回答
- 它**过不了 CI**,而报的错是"文件不存在",指不到"这条路径本来就不该在这里"
- 带用户名的那种(`C:\\Users\\<某人>`)顺带还泄露了一个人名

所以这条检查扫的是**盘符开头的绝对路径**和 MSYS 那种 `/d/...`,
以及任何 `/Users/<名字>`。

**服务器上的路径不算。** `/var/backups/lifein`、`/etc/systemd/...` 是部署目标,
不是某台开发机 —— 而且它们都有环境变量能覆盖(见 `scripts/backup.sh`)。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CHECKED_SUFFIXES = {
    ".md", ".py", ".sh", ".cmd", ".kt", ".kts",
    ".toml", ".ini", ".cfg", ".yml", ".yaml", ".sql",
}

MACHINE_PATH = re.compile(
    r"""
    (?:^|[^A-Za-z0-9_])[A-Za-z]:[\\/]      # C:\ 或 D:/
  | /(?:c|d|e)/[A-Za-z]                     # MSYS 的 /d/environment
  | /Users/[A-Za-z]                         # macOS 家目录
  | /home/[A-Za-z]                          # Linux 家目录
    """,
    re.VERBOSE,
)

ALLOWED = (
    # 这份文件自己:上面那个正则里就有这些字样
    "tests/test_no_machine_paths.py",
)


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"跑不了 git:{result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


def test_no_tracked_file_hardcodes_a_developer_machine_path():
    """**红了不要把路径改成你自己那台的。**

    改成变量:文档里写 `<你的 Android SDK>`,脚本里读 `ANDROID_HOME`,
    要落到磁盘上的东西放进 `local.properties` 或 `.env`(两个都不进版本库)。
    """
    offenders: list[str] = []
    for name in tracked_files():
        if name in ALLOWED or Path(name).suffix not in CHECKED_SUFFIXES:
            continue
        path = ROOT / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if MACHINE_PATH.search(line):
                offenders.append(f"{name}:{number}: {line.strip()[:100]}")

    assert offenders == [], (
        "这些地方写死了某一台机器上的路径,而这个项目是多机器开发的:\n  "
        + "\n  ".join(offenders)
        + "\n改成 JAVA_HOME / ANDROID_HOME / local.properties / .env,不要改成你那台的路径"
    )


def test_local_properties_is_not_tracked():
    """**上面那条的具体化。**

    `android/local.properties` 里就是一行 `sdk.dir=<某台机器上的路径>` ——
    它一旦进了版本库,别人 clone 下来第一件事就是编译失败,
    而报的错是 SDK 找不到,指不到"这个文件本来就不该在这里"。
    """
    assert "android/local.properties" not in tracked_files()
