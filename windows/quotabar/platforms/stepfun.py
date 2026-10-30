# -*- coding: utf-8 -*-
"""Stepfun(阶跃星辰)取数服务。移植自 StepFunPlatformService.swift。

契约要点(gRPC-Connect over HTTP/JSON,非公开 API):
- POST {api_base_url}/{Method}:QueryStepPlanRateLimit(主指标)+ GetStepPlanStatus(订阅状态)
- 必须带 5 个头:整串 Cookie、oasis-appid: 10300、oasis-platform: web、
  oasis-webid(从 Cookie 里拆出,服务端据此做令牌挪用校验)、Connect-Protocol-Version: 1
- body 是字面量 "{}"
- credit 套餐族取 subscription_credit_left_rate(0~1);否则降级 five_hour/weekly 用量率
- 订阅状态请求失败不阻断主流程(静默降级只显示余量)
- subscription_credit_reset_time / expired_at 是 Unix 秒,可能是字符串也可能是数字,
  双读;时间戳要过合理性区间(1.6e9~4e9)挡毫秒漂移
"""

from __future__ import annotations

import math
import time

from ..models import (
    LABEL_CREDITS,
    LABEL_FIVE_HOUR,
    LABEL_WEEKLY_LIMIT,
    ErrorKind,
    PlatformConfigData,
    PlatformError,
    PlatformType,
    PlatformUsageData,
    UsageMetric,
)
from ..net import NetworkFunc, Request, Response
from ._base import ServiceCache, parse_json, send, to_float

CACHE_TTL = 300.0  # 秒;非公开接口,刻意放缓(与 macOS 版一致)
OASIS_APPID = "10300"
#: 时间戳合理性区间(Unix 秒):挡住毫秒漂移出来的 1970 假日期
TS_SANITY_MIN, TS_SANITY_MAX = 1.6e9, 4.0e9


class StepFunPlatformService:
    platform_type = PlatformType.STEPFUN

    def __init__(self):
        self._cache = ServiceCache()

    def clear_cache(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------ 凭据解析

    @staticmethod
    def extract_webid(cookie: str) -> str:
        """从整串 cookie 里拆 Oasis-Webid(大小写不敏感);拆不到返回空串"""
        for pair in cookie.split(";"):
            name, _, value = pair.strip().partition("=")
            if name.lower() == "oasis-webid":
                return value.strip()
        return ""

    def _headers(self, config: PlatformConfigData, webid: str) -> dict[str, str]:
        return {
            "Cookie": config.api_key,
            "oasis-appid": OASIS_APPID,
            "oasis-platform": "web",
            "oasis-webid": webid,
            "Connect-Protocol-Version": "1",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------ 取数

    def fetch_usage(self, config: PlatformConfigData, network: NetworkFunc) -> PlatformUsageData:
        cached = self._cache.read(CACHE_TTL)
        if cached is not None:
            return cached
        ver = self._cache.begin()

        webid = self.extract_webid(config.api_key)
        if not webid:
            # 凭据早失败:格式不对就不发请求
            raise PlatformError(ErrorKind.API, "error.stepfun.cookieFormat")
        headers = self._headers(config, webid)

        metrics = self._fetch_rate_limit(config, network, headers)
        expired = self._fetch_subscription_expired(config, network, headers)

        result = PlatformUsageData(
            platform=self.platform_type,
            instance_id=config.instance_id,
            display_name=config.display_name,
            metrics=metrics,
            is_healthy=not expired,  # 订阅失效即红,余量百分比不参与健康度
        )
        self._cache.write(result, ver)
        return result

    def _post(self, config: PlatformConfigData, network: NetworkFunc, headers: dict, method: str) -> Response:
        return send(
            network,
            Request(
                method="POST",
                url=f"{config.resolved_base_url()}/{method}",
                headers=headers,
                timeout=10.0,
                body="{}",
            ),
            body_in_error=True,
            # Stepfun 的 403 = cookie 过期/被顶/风控拒绝,和 401 一样是凭据问题
            forbid_as_unauthorized=True,
        )

    def _fetch_rate_limit(self, config, network, headers) -> list[UsageMetric]:
        body = parse_json(
            self._post(config, network, headers, "QueryStepPlanRateLimit").text
        )
        status = body.get("status")
        if status != 1:
            raise PlatformError(ErrorKind.API, body.get("desc") or f"status {status}")

        rate = body.get("plan_credit_rate_limit")
        if not isinstance(rate, dict):
            rate = {}
        credit_left = rate.get("subscription_credit_left_rate")
        if credit_left is not None:
            # credit 套餐族:单指标 credits
            value = to_float(credit_left)
            if value is None or not math.isfinite(value * 100):
                # 乘 100 之后仍要有限:1e308 * 100 = inf,会一路炸到渲染层
                raise PlatformError(ErrorKind.INVALID_RESPONSE)
            return [
                UsageMetric(
                    label=LABEL_CREDITS,
                    current_value=value * 100,
                    total_value=100,
                    unit="%",
                    reset_time=_sane_unix_seconds(
                        rate.get("subscription_credit_reset_time")
                    ),
                )
            ]
        # 降级:非 credit 套餐显示 5 小时/周用量剩余率
        metrics = []
        for key, label in (
            ("five_hour_usage_left_rate", LABEL_FIVE_HOUR),
            ("weekly_usage_left_rate", LABEL_WEEKLY_LIMIT),
        ):
            raw = body.get(key)
            if raw is None:
                continue
            value = to_float(raw)
            if value is None or not math.isfinite(value * 100):
                continue  # 类型不对或乘 100 后溢出:整条跳过,不谎报
            metrics.append(
                UsageMetric(
                    label=label,
                    current_value=value * 100,
                    total_value=100,
                    unit="%",
                )
            )
        if not metrics:
            raise PlatformError(ErrorKind.INVALID_RESPONSE)
        return metrics

    def _fetch_subscription_expired(self, config, network, headers) -> bool:
        """订阅是否失效;任何失败都静默降级(返回 False,健康度不受影响)"""
        try:
            body = parse_json(
                self._post(config, network, headers, "GetStepPlanStatus").text
            )
            if body.get("status") != 1:
                return False
            sub = body.get("subscription") or {}
            status_val = sub.get("status")
            # status 缺失不算失效(与 Swift 版 sub.status != nil 判断一致)
            if status_val is not None and status_val != 1:
                return True  # 先信 status 字段,不信时间戳(停订时 expired_at 常仍在未来)
            expired_at = _sane_unix_seconds(sub.get("expired_at"))
            return expired_at is not None and expired_at < time.time()
        except Exception:
            return False


def _sane_unix_seconds(value) -> float | None:
    """Unix 秒时间戳:兼容字符串/数字双形态,并过滤不合理区间。

    macOS 版对**重置时间**也做了同样的区间过滤,但 `expired_at` 那条路径没有
    (StepFunPlatformService.swift:134-135)。所以真实差异是:
    expired_at 是个很小的垃圾值(如 0)时,macOS 判「已过期」标红,
    Windows 过滤掉判「未到期」。这里**有意更保守**(不拿明显非法的值判失效),
    别当成 bug 回改。
    """
    if value is None:
        return None
    ts = to_float(value)
    if ts is None:
        return None
    if not (TS_SANITY_MIN < ts < TS_SANITY_MAX):
        return None
    return ts
