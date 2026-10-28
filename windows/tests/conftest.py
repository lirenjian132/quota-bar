# -*- coding: utf-8 -*-
"""pytest 公共夹具:让 tests 能 import quotabar,并提供 Mock 网络与临时配置目录。"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from quotabar.net import Request, Response  # noqa: E402


class FakeNetwork:
    """按序返回预设响应,并记录发出的请求供断言"""

    def __init__(self, *responses: Response):
        self.responses = list(responses)
        self.requests: list[Request] = []

    def __call__(self, req: Request) -> Response:
        self.requests.append(req)
        if not self.responses:
            raise AssertionError("没有更多预设响应了")
        return self.responses.pop(0)

    @property
    def call_count(self) -> int:
        return len(self.requests)


def json_response(payload: dict, status_code: int = 200) -> Response:
    return Response(status_code=status_code, text=json.dumps(payload))


@pytest.fixture
def config(tmp_path):
    """隔离数据目录的 ConfigService"""
    from quotabar.config import ConfigService

    return ConfigService(home=str(tmp_path / "home"))


@pytest.fixture
def fake_network_factory():
    return FakeNetwork
