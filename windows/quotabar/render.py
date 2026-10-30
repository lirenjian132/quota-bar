# -*- coding: utf-8 -*-
"""渲染纯函数层:托盘图标文本、状态点颜色、指标数值格式化。

对齐 macOS 版 Views/StatusBarView.swift 里的 StatusBarViewHelper(抽成纯函数便于单测,
不依赖 Qt 运行时)。
"""

from __future__ import annotations

import math
import time
from typing import Optional

from .models import (
    LABEL_WEEKLY_UNLIMITED,
    PlatformUsageData,
    UsageMetric,
    label_family,
)

COLOR_GRAY = "gray"    # 无数据
COLOR_RED = "red"
COLOR_YELLOW = "yellow"
COLOR_GREEN = "green"


def filter_metrics(metrics: list[UsageMetric], enabled_labels: list[str]) -> list[UsageMetric]:
    """按勾选的 label 过滤指标(最多 2 个),带同族匹配、去重与回退。

    - 同族匹配:勾了 weekly_limit,返回的 weekly_limit_boosted/unlimited 也能命中
    - 去重:勾了两个同族 label(如周额度+周额度加成)时,同一个 metric 只取一次
    - 全无交集:回退前 2 个 metric(防状态栏恒显 "--")
    """
    picked: list[UsageMetric] = []
    seen: set[int] = set()
    for want in enabled_labels[:2]:
        fam = label_family(want)
        for m in metrics:
            if m.label in fam and id(m) not in seen:
                picked.append(m)
                seen.add(id(m))
                break
    if picked:
        return picked[:2]
    return metrics[:2]


def _ratio(metric: UsageMetric) -> Optional[float]:
    """剩余比例(0~1);无 total 的绝对量型返回 None"""
    if metric.total_value is None or metric.total_value <= 0:
        return None
    ratio = metric.current_value / metric.total_value
    # 非有限值(inf/NaN)会让后面的 int() 抛 OverflowError/ValueError,
    # 从取数一路炸到界面重绘——这里直接当作无数据
    if not math.isfinite(ratio):
        return None
    return ratio


def _safe_int(value: float) -> Optional[int]:
    """转 int;非有限值或离谱数值返回 None(调用方显示 --)

    离谱数值(>1e15)来自「current=1e308 / total=1e-308」这类畸形比例,
    转成整数是几百位数字,直接塞进 tooltip 会把界面撑爆。
    """
    if not math.isfinite(value) or abs(value) > 1e15:
        return None
    try:
        return int(value)
    except (ValueError, OverflowError):
        return None


def status_color(data: Optional[PlatformUsageData]) -> str:
    """状态点颜色;判定顺序与 macOS 版一致,顺序不可变:
    无数据→灰;空 metrics→灰;不健康→红;首指标比例<10%红、<50%黄、否则绿;
    绝对量型(余额)看到期时间 <3 天红、<7 天黄。
    """
    if data is None or not data.metrics:
        return COLOR_GRAY
    if not data.is_healthy:
        return COLOR_RED
    first = data.metrics[0]
    ratio = _ratio(first)
    if ratio is not None:
        if ratio < 0.1:
            return COLOR_RED
        if ratio < 0.5:
            return COLOR_YELLOW
        return COLOR_GREEN
    if first.reset_time is not None:
        days_left = (first.reset_time - time.time()) / 86400
        if days_left < 3:
            return COLOR_RED
        if days_left < 7:
            return COLOR_YELLOW
    return COLOR_GREEN


def format_balance(value: float) -> str:
    """余额缩写:>=1000 显示 "1.2K",否则取整"""
    if not math.isfinite(value):
        return "--"  # inf/NaN:不能进格式化
    if value >= 1000:
        return f"{value / 1000:.1f}K"
    return f"{value:.0f}"


def tray_text(metric: Optional[UsageMetric], display_mode: str) -> str:
    """托盘图标数字:百分比向零截断不带 %(已用/剩余口径,对齐 Swift Int())、余额 K 缩写、无限显示 ∞"""
    if metric is None:
        return "--"
    if metric.unit == "unlimited" or metric.label == LABEL_WEEKLY_UNLIMITED:
        return "∞"
    ratio = _ratio(metric)
    if ratio is not None:
        pct = (1 - ratio) * 100 if display_mode == "used" else ratio * 100
        number = _safe_int(pct)
        return str(number) if number is not None else "--"
    if metric.unit == "CNY":
        return format_balance(metric.current_value)
    number = _safe_int(metric.current_value)
    return str(number) if number is not None else "--"


def format_credits_count(value: float, locale: str) -> str:
    """大数次数格式化,对齐 Swift formatCredits:Int 截断 + 缩写两位小数(中文万/亿,英文 K/M)"""
    if not math.isfinite(value) or abs(value) > 1e15:
        # inf/NaN:转 int 会抛异常;离谱数值(1e308 这种畸形比例)会变成几百位数字
        return "--"
    v = int(value)
    if locale.startswith("zh"):
        if v >= 100_000_000:
            return f"{v / 1e8:.2f}亿"
        if v >= 10_000:
            return f"{v / 1e4:.2f}万"
        return str(v)
    if v >= 1_000_000:
        return f"{v / 1e6:.2f}M"
    if v >= 1_000:
        return f"{v / 1e3:.2f}K"
    return str(v)


def metric_detail_text(metric: UsageMetric, locale: str) -> str:
    """详情卡片数值文本(对齐 macOS 弹窗口径):
    - 百分比型 → "80%"(剩余口径)
    - 次数型 → "932/1000 times"(两侧都做大数缩写,对齐 Swift formatCredits)
    - 余额型 → "254 CNY"
    """
    if not math.isfinite(metric.current_value):
        return "--"  # inf/NaN:没有可显示的数值
    ratio = _ratio(metric)
    if metric.unit == "%" and ratio is not None:
        number = _safe_int(ratio * 100)
        return f"{number}%" if number is not None else "--"
    # total 必须为正才有意义(total=0 时显示 "5/0" 是胡话,对齐 macOS 的 total > 0 判定)
    if metric.total_value is not None and metric.total_value > 0:
        cur = format_credits_count(metric.current_value, locale)
        total = format_credits_count(metric.total_value, locale)
        if cur == "--" or total == "--":
            return "--"  # 有一侧是离谱数值:整条显示无意义
        return f"{cur}/{total} {metric.unit}".strip()
    return f"{metric.current_value:.0f} {metric.unit}".strip()


def metric_value_text(metric: UsageMetric, display_mode: str, locale: str) -> str:
    """面板/浮窗里的指标数值文本(按显示模式区分已用/剩余):
    - 百分比型 → 按模式取百分比(浮窗与面板口径一致)
    - 无限型 → ∞
    - 次数/余额型 → 详情口径(cur/total 单位),不能当百分比渲染
    """
    if metric.unit == "unlimited" or metric.label == LABEL_WEEKLY_UNLIMITED:
        return "∞"
    if metric.unit == "%":
        return f"{tray_text(metric, display_mode)}%"
    return metric_detail_text(metric, locale)


def reset_countdown(reset_time: float, now: Optional[float] = None) -> tuple[str, Optional[tuple[int, int]]]:
    """重置倒计时。返回 (文案 kind, 载荷):
    - "soon":即将重置(≤1 分钟)
    - "hm":(时, 分),还剩 H 小时 M 分(<25 小时)
    - "days":(天, 小时),跨天(≥25 小时,对齐 Swift 的 hours > 24 判定)

    Swift 侧按整小时算(hours = Int(interval)/3600,hours > 24 才切天),
    所以切天点是剩余 25 小时整,不是 24 小时零 1 分——对齐它。
    """
    now = now if now is not None else time.time()
    remain = reset_time - now
    if remain <= 60:
        return "soon", None
    total_min = int(remain // 60)
    days, rest_min = divmod(total_min, 1440)
    if total_min >= 25 * 60:
        return "days", (days, rest_min // 60)
    return "hm", divmod(total_min, 60)
