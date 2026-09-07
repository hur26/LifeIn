"""运维命令的测试。

不测 argparse 本身,测两件真会出事的:授权码不进命令行参数、
换凭据时旧的会被吊销。
"""

from __future__ import annotations

import inspect

from lifein import admin


def test_auth_code_is_not_a_command_line_argument():
    """命令行参数会进 shell history、进 ps、进会话录制,三处都清不干净。"""
    parser = admin.build_parser()
    actions = {
        action.dest
        for sub in parser._subparsers._group_actions  # noqa: SLF001
        for choice in sub.choices.values()
        for action in choice._actions  # noqa: SLF001
    }
    for forbidden in ("auth_code", "password", "secret", "token"):
        assert forbidden not in actions


def test_set_imap_reads_from_getpass():
    source = inspect.getsource(admin.cmd_set_imap)
    assert "getpass" in source


def test_set_imap_revokes_the_old_credential_first():
    """同一类凭据留着两条有效的,读到哪条取决于排序 ——
    那是将来某天"改了密码怎么还是旧的"的根源。"""
    source = inspect.getsource(admin.cmd_set_imap)
    assert source.index("revoke_credential") < source.index("put_credential")


def test_key_status_exits_nonzero_when_rotation_is_incomplete():
    # 轮换没做完是个待办状态,退出码要能被脚本判断
    source = inspect.getsource(admin.cmd_key_status)
    assert "return 1" in source


def test_every_subcommand_has_a_handler():
    parser = admin.build_parser()
    for sub in parser._subparsers._group_actions:  # noqa: SLF001
        for name, choice in sub.choices.items():
            assert choice.get_default("func") is not None, f"{name} 没有绑定处理函数"
