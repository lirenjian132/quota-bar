# -*- coding: utf-8 -*-
"""网络抽象层:取数服务通过可注入的 network 函数发请求,便于测试 Mock。

对齐 macOS 版的 NetworkService 协议(URLSession 封装可注入)。
"""

from __future__ import annotations

import dataclasses
from typing import Callable, Optional

import requests


@dataclasses.dataclass
class Request:
    """一次 HTTP 请求的完整描述"""

    method: str = "GET"
    url: str = ""
    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    timeout: float = 10.0
    body: Optional[str] = None


@dataclasses.dataclass
class Response:
    """HTTP 响应的最小封装(服务层只关心状态码与文本体)"""

    status_code: int
    text: str


NetworkFunc = Callable[[Request], Response]


def requests_network(req: Request) -> Response:
    """默认实现:走 requests 库"""
    resp = requests.request(
        req.method,
        req.url,
        headers=req.headers,
        timeout=req.timeout,
        data=req.body.encode("utf-8") if req.body is not None else None,
    )
    return Response(status_code=resp.status_code, text=resp.text)
