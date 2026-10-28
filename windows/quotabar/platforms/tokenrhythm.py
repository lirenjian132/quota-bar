# -*- coding: utf-8 -*-
"""基元律动(TokenRhythm)取数服务。移植自 TokenRhythmPlatformService.swift。

契约要点:
- GET {api_base_url}(完整端点 /api/wallet/summary)
- 鉴权:Cookie: tr_session=<裸 session 值>(auth_prefix 由模板拼接,apiKey 只存裸值)
- data.availableBalanceCny 是「字符串形式的 8 位小数」,直接 float() 解析
- 辅端点:把 URL 里的 /wallet/summary 字符串替换成 /wallet/expiring-credits,
  取最早赠金到期时刻;任何失败都静默降级(纯余额显示)
- ISO8601 时间带毫秒(...56.281Z),解析需兼容
- 健康度:余额 >= 10 元 且 最早到期不在 3 天内(到期警示优先于低余额)
"""

from __future__ import annotations

import time
from datetime import datetime

from ..models import (
    LABEL_BALANCE,
    ErrorKind,
    PlatformConfigData,
    PlatformError,
    PlatformType,
    PlatformUsageData,
    UsageMetric,
)
from ..net import NetworkFunc, Request
from ._base import ServiceCache, parse_json, send

CACHE_TTL = 300.0  # 秒;中转站接口,刻意放缓防风控(与 macOS 版一致)


class TokenRhythmPlatformService:
    platform_type = PlatformType.TOKENRHYTHM

    def __init__(self):
        self._cache = ServiceCache()

    def clear_cache(self) -> None:
        self._cache.clear()

    def fetch_usage(self, config: PlatformConfigData, network: NetworkFunc) -> PlatformUsageData:
        cached = self._cache.read(CACHE_TTL)
        if cached is not None:
            return cached
        ver = self._cache.begin()

        headers = {
            "Cookie": f"{config.auth_prefix}{config.api_key}",
            "Accept": "application/json",
        }
        resp = send(
            network,
            Request(
                method="GET",
                url=config.resolved_base_url(),
                headers=headers,
                timeout=10.0,
            ),
            body_in_error=True,
        )
        balance, next_expiry = self._parse_main(parse_json(resp.text))
        # 辅端点:查不到也不影响主流程
        if next_expiry is None:
            next_expiry = self._fetch_next_expiry(config, network, headers)

        healthy = balance >= 10.0 and not (
            next_expiry is not None and next_expiry - time.time() < 3 * 86400
        )
        result = PlatformUsageData(
            platform=self.platform_type,
            instance_id=config.instance_id,
            display_name=config.display_name,
            metrics=[
                UsageMetric(
                    label=LABEL_BALANCE,
                    current_value=balance,
                    total_value=None,
                    unit="CNY",
                    reset_time=next_expiry,
                )
            ],
            is_healthy=healthy,
        )
        self._cache.write(result, ver)
        return result

    def _parse_main(self, body: dict) -> tuple[float, float | None]:
        if body.get("code") not in (0, "0", None):
            msg = body.get("message") or f"code {body.get('code')}"
            raise PlatformError(ErrorKind.API, str(msg))
        data = body.get("data") or {}
        raw = data.get("availableBalanceCny")
        try:
            balance = float(raw)
        except (TypeError, ValueError) as e:
            raise PlatformError(ErrorKind.INVALID_RESPONSE) from e
        # 主响应里若直接带到期信息则顺带取(当前没有,留给辅端点)
        return balance, None

    def _fetch_next_expiry(self, config, network, headers) -> float | None:
        """最早赠金到期时刻;URL 由主端点字符串替换派生,失败返回 None"""
        url = config.resolved_base_url()
        aux_url = url.replace("/wallet/summary", "/wallet/expiring-credits")
        if aux_url == url:
            return None  # 替换是 no-op,说明端点形态变了,放弃
        try:
            resp = send(
                network,
                Request(method="GET", url=aux_url, headers=headers, timeout=10.0),
            )
            body = parse_json(resp.text)
            if body.get("code") not in (0, "0", None):
                return None
            raw = (body.get("data") or {}).get("summary", {}).get("nextExpiryAt")
            return _parse_iso8601(raw)
        except Exception:
            return None


def _parse_iso8601(raw) -> float | None:
    """解析 ISO8601(带毫秒,如 2026-09-26T15:20:56.281Z)为 Unix 秒"""
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        return None
