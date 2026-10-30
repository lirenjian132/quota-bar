# -*- coding: utf-8 -*-
"""QuotaBar Windows 版 — 核心模型层。

对齐 macOS 版 Models/PlatformProtocol.swift 的语义:
- PlatformType:平台枚举,raw 值与 Swift 版持久化 id 一致
- UsageMetric / PlatformUsageData:用量指标与一次取数结果
- PlatformConfigData:单账号实例的取数配置(URL/鉴权头/凭据形态)
- RefreshInterval:刷新间隔档位,raw 值与 Swift 版一致
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PlatformType(str, Enum):
    """平台类型;value 即持久化用的 raw id(与 Swift 版一致)"""

    MINIMAX_CN = "minimax_cn"
    GLM_CN = "glm_cn"
    TOKENRHYTHM = "tokenrhythm"
    STEPFUN = "stepfun"

    def display_name(self) -> str:
        """平台默认显示名(用户可对账号实例自定义覆盖)"""
        return {
            PlatformType.MINIMAX_CN: "MiniMax",
            PlatformType.GLM_CN: "GLM",
            PlatformType.TOKENRHYTHM: "基元律动",
            PlatformType.STEPFUN: "Stepfun",
        }[self]

    def cookie_based(self) -> bool:
        """cookie 型平台:凭据是登录 cookie,失效后走「重新登录」续期"""
        return self in (PlatformType.TOKENRHYTHM, PlatformType.STEPFUN)


# ---------------------------------------------------------------- 指标 label 常量(与 Swift 版一致)

LABEL_FIVE_HOUR = "five_hour"
LABEL_WEEKLY_LIMIT = "weekly_limit"
LABEL_WEEKLY_BOOSTED = "weekly_limit_boosted"
LABEL_WEEKLY_UNLIMITED = "weekly_limit_unlimited"
LABEL_MCP_MONTHLY = "mcp_monthly"
LABEL_CREDITS = "credits"
LABEL_BALANCE = "balance"

#: 每个平台允许勾选的指标清单(对齐 ConfigService.availableMetricLabels)
AVAILABLE_METRIC_LABELS: dict[PlatformType, list[str]] = {
    PlatformType.MINIMAX_CN: [
        LABEL_FIVE_HOUR,
        LABEL_WEEKLY_LIMIT,
        LABEL_WEEKLY_BOOSTED,
        LABEL_WEEKLY_UNLIMITED,
    ],
    PlatformType.GLM_CN: [LABEL_FIVE_HOUR, LABEL_WEEKLY_LIMIT, LABEL_MCP_MONTHLY],
    PlatformType.TOKENRHYTHM: [LABEL_BALANCE],
    PlatformType.STEPFUN: [LABEL_CREDITS, LABEL_FIVE_HOUR, LABEL_WEEKLY_LIMIT],
}

#: 每平台默认勾选指标(对齐 ConfigService.defaultEnabledMetrics)
DEFAULT_ENABLED_METRICS: dict[PlatformType, list[str]] = {
    PlatformType.MINIMAX_CN: [LABEL_FIVE_HOUR],
    PlatformType.GLM_CN: [LABEL_FIVE_HOUR, LABEL_WEEKLY_LIMIT],
    PlatformType.TOKENRHYTHM: [LABEL_BALANCE],
    PlatformType.STEPFUN: [LABEL_CREDITS],
}

#: 同族指标:勾选族名后,套餐状态切换(普通/加成/无限)仍能渲染出来
WEEKLY_FAMILY = {
    LABEL_WEEKLY_LIMIT,
    LABEL_WEEKLY_BOOSTED,
    LABEL_WEEKLY_UNLIMITED,
}


def label_family(label: str) -> set[str]:
    """返回与该 label 同族的集合(非周额度族则只含自身)"""
    return WEEKLY_FAMILY if label in WEEKLY_FAMILY else {label}


# ---------------------------------------------------------------- 错误体系

class ErrorKind(Enum):
    NOT_CONFIGURED = "notConfigured"
    INVALID_RESPONSE = "invalidResponse"
    NETWORK = "networkError"
    UNAUTHORIZED = "unauthorized"
    DECODING = "decodingError"
    API = "apiError"


class PlatformError(Exception):
    """平台取数错误;kind 决定 UI 提示文案的分派(api/unauthorized 分 cookie 型与 key 型)"""

    def __init__(self, kind: ErrorKind, message: str = ""):
        self.kind = kind
        # 消息一律转 str:上层会拿它当 i18n 键查表,非字符串(dict/list)会抛 TypeError
        self.message = str(message) if message else kind.value
        super().__init__(f"[{kind.value}] {message}")


# ---------------------------------------------------------------- 取数结果

@dataclass
class UsageMetric:
    """单个用量指标。

    - 百分比型:current_value=剩余百分比,total_value=100,unit="%"
    - 次数型(MCP 月度):current/total 为次数,unit="times"
    - 余额型:current_value=金额,total_value=None,unit="CNY"
    - 无限型:current_value=0,total_value=None,unit="unlimited"
    """

    label: str
    current_value: float
    total_value: Optional[float] = None
    unit: str = "%"
    reset_time: Optional[float] = None  # Unix 秒;重置时刻或最早到期时刻


@dataclass
class PlatformUsageData:
    """一次取数的完整结果(单账号实例)"""

    platform: PlatformType
    instance_id: str
    display_name: str
    metrics: list[UsageMetric] = field(default_factory=list)
    last_updated: float = field(default_factory=time.time)
    is_healthy: bool = True


@dataclass
class PlatformConfigData:
    """单账号实例的取数配置(由 ConfigStore 持久化层拼装)"""

    platform_type: PlatformType
    instance_id: str
    display_name: str
    api_base_url: str
    auth_header: str          # "Authorization" 或 "Cookie"
    auth_prefix: str          # "Bearer " / "" / "tr_session="
    api_key: str              # API key、整串 cookie 或裸 session 值(形态由模板决定)
    region: Optional[str] = None
    api_base_url_international: Optional[str] = None

    def resolved_base_url(self) -> str:
        """按区域选 base url:国际区取国际 URL,缺失时回退主 URL"""
        if self.region == "international":
            return self.api_base_url_international or self.api_base_url
        return self.api_base_url


@dataclass
class PlatformInstance:
    """账号实例(注册表条目)"""

    id: str
    platform_type: PlatformType
    display_name: str


class RefreshInterval(Enum):
    """刷新间隔档位;value 与 Swift 版持久化 raw 值一致"""

    THIRTY_SECONDS = ("thirtySeconds", 30)
    ONE_MINUTE = ("oneMinute", 60)
    THREE_MINUTES = ("threeMinutes", 180)
    FIVE_MINUTES = ("fiveMinutes", 300)
    TEN_MINUTES = ("tenMinutes", 600)

    def __new__(cls, raw: str, seconds: int):
        obj = object.__new__(cls)
        obj._value_ = raw
        obj.seconds = seconds
        return obj

    @classmethod
    def from_raw(cls, raw: Optional[str]) -> "RefreshInterval":
        """从持久化 raw 值还原;非法/缺失回落默认 1 分钟"""
        for item in cls:
            if item.value == raw:
                return item
        return cls.ONE_MINUTE
