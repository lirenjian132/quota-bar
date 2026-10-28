# -*- coding: utf-8 -*-
"""平台服务公共基座:线程安全的取数缓存 + 统一 HTTP 错误分派。"""

from __future__ import annotations

import threading
import time
from typing import Optional

from ..models import ErrorKind, PlatformError
from ..net import NetworkFunc, Request, Response


class ServiceCache:
    """单账号实例的取数缓存:value + 时间戳,过期即失效(对齐 PlatformUsageCache)。

    带代次(version)守卫:取数线程先 begin() 记下代次,写回时若代次已变
    (取数期间发生过 clear——换 key/立即刷新),过期结果直接丢弃。
    防「旧 key 的在途请求晚到,把旧账号数据写进缓存污染 5 分钟」的竞态。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._value: Optional[object] = None
        self._ts: float = 0.0
        self._version: int = 0

    def begin(self) -> int:
        """取数前记录当前代次"""
        with self._lock:
            return self._version

    def read(self, timeout: float):
        with self._lock:
            if self._value is None:
                return None
            if time.time() - self._ts > timeout:
                return None
            return self._value

    def write(self, value, version: Optional[int] = None) -> None:
        with self._lock:
            if version is not None and version != self._version:
                return  # 取数期间缓存被清除(换 key 等),丢弃过期结果
            self._value = value
            self._ts = time.time()

    def clear(self) -> None:
        with self._lock:
            self._value = None
            self._ts = 0.0
            self._version += 1


def send(network: NetworkFunc, req: Request, body_in_error: bool = False) -> Response:
    """发请求并统一分派 HTTP 错误:
    401/403 → 未授权(cookie 型平台 UI 会引导重新登录);其他非 2xx → 网络错误。
    """
    resp = network(req)
    if resp.status_code in (401, 403):
        raise PlatformError(ErrorKind.UNAUTHORIZED)
    if not 200 <= resp.status_code < 300:
        detail = f"HTTP {resp.status_code}"
        if body_in_error and resp.text:
            detail += f": {resp.text[:200]}"
        raise PlatformError(ErrorKind.NETWORK, detail)
    return resp


def parse_json(text: str) -> dict:
    """解析 JSON body;失败抛解析错误"""
    import json

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise PlatformError(ErrorKind.DECODING, str(e)) from e
