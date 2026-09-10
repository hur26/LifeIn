"""HTTP 入口自己的决定。

**这个文件此前几乎全是企微回调的测试**,而企微整个退出了
([ADR-026](../docs/04-tech-decisions.md#adr-026--企业微信整个退出消息面只剩-ilink-与邮件))
—— `/wecom/callback` 那两条路由连同它们的握手、签名校验、解密一起没了。

那些用例验的东西并没有消失,只是搬了家:入站现在走 iLink 的长轮询,
而"一条消息处理失败不该让循环退出""游标要持久化""会话过期要停下来告警"
都在 [`test_weixin_inbox_job.py`](test_weixin_inbox_job.py) 里。

**所以这里只剩两条**,它们是这一层真正独有的:健康检查不依赖任何东西,
以及交互式文档不许开着。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lifein.api.app import create_app
from lifein.bootstrap import Services


class _NullSession:
    """什么都不做的会话工厂。这两条用例一次库都不碰。"""

    def __enter__(self):
        return None

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("lifein.api.app.session_scope", lambda: _NullSession())
    # 入站线程会去列用户、去连 iLink —— 这两条用例不需要它
    monkeypatch.setattr("lifein.api.app._start_inboxes", lambda services: [])
    app = create_app(Services(settings=None, llm=None, channel=None, alerter=None))
    return TestClient(app)


def test_healthz_needs_nothing(client):
    """**健康检查不许依赖数据库或任何外部服务。**

    依赖了的话它回答的就不是"进程还活着",而是"整条链路都好着" ——
    而那时它变成了一个会跟着别人一起挂的探针,监控会在真正该报警之前
    先被它自己的噪音淹掉。
    """
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_docs_endpoints_are_disabled(client):
    """**交互式文档不许开着。**

    `/docs` 会把全部路由、入参 schema 和示例摆出来 —— 而这个服务上
    每一条路由都通向某个人的私人数据。少一份给外面的地图。
    """
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
