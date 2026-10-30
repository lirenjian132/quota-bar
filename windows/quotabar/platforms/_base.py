# -*- coding: utf-8 -*-
"""平台服务公共基座:线程安全的取数缓存 + 统一 HTTP 错误分派。"""

from __future__ import annotations

import math
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


def send(
    network: NetworkFunc,
    req: Request,
    body_in_error: bool = False,
    forbid_as_unauthorized: bool = False,
) -> Response:
    """发请求并统一分派 HTTP 错误:
    401 → 未授权(cookie 型平台 UI 会引导重新登录);其他非 2xx → 网络错误。

    403 只有在「403 即凭据问题」的平台才判未授权(Stepfun:cookie 过期/被风控拒绝);
    其余平台的 403 多半是 WAF 拦截,判成「API Key 无效」会误导用户换掉好用的 key
    (与 macOS 版一致:GLM/MiniMax/基元律动只认 401)。
    """
    resp = network(req)
    if resp.status_code == 401 or (resp.status_code == 403 and forbid_as_unauthorized):
        raise PlatformError(ErrorKind.UNAUTHORIZED)
    if not 200 <= resp.status_code < 300:
        detail = f"HTTP {resp.status_code}"
        if body_in_error and resp.text:
            detail += f": {resp.text[:200]}"
        raise PlatformError(ErrorKind.NETWORK, detail)
    return resp


def to_float(value, default=None):
    """宽松地转 float:数字与数字字符串都接受,其它返回 default。

    服务端偶尔把数字字段序列化成字符串(Stepfun 的 expired_at 就是双形态),
    直接 float() 会抛裸 ValueError/TypeError,最终被 manager 兜成「网络错误」——
    用户看到的是误导文案。这里统一收敛:转不了就返回 default 由调用方决定跳过。

    NaN / Infinity 也要挡掉:json.loads 默认接受这两个字面量,而渲染层的
    int(NaN) 会抛 ValueError、int(inf) 会抛 OverflowError——从取数一路炸到
    界面重绘,托盘与面板会静默停止更新。
    """
    if isinstance(value, bool):
        return default
    try:
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            number = float(value.strip())
        else:
            return default
    except (ValueError, OverflowError):
        # OverflowError:超出 float 上限的巨整数(401 位字面量)转不动
        return default
    if not math.isfinite(number):
        return default
    return number


def parse_json(text: str) -> dict:
    """解析 JSON body;失败抛解析错误。

    根不是对象(list/字符串/数字/null)也算无效响应——各平台紧接着就要
    body.get(...),让裸 AttributeError 冒到上层只会被显示成误导性的「网络错误」。
    """
    import json

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise PlatformError(ErrorKind.DECODING, str(e)) from e
    if not isinstance(data, dict):
        raise PlatformError(ErrorKind.INVALID_RESPONSE)
    return data
