# -*- coding: utf-8 -*-
"""渲染纯函数测试:状态色判定顺序、托盘文本口径、指标过滤同族匹配、格式化。"""

import time

from quotabar.models import (
    LABEL_BALANCE,
    LABEL_FIVE_HOUR,
    LABEL_WEEKLY_BOOSTED,
    LABEL_WEEKLY_LIMIT,
    LABEL_WEEKLY_UNLIMITED,
    PlatformType,
    PlatformUsageData,
    UsageMetric,
)
from quotabar.render import (
    COLOR_GRAY,
    COLOR_GREEN,
    COLOR_RED,
    COLOR_YELLOW,
    filter_metrics,
    format_balance,
    format_credits_count,
    metric_detail_text,
    status_color,
    tray_text,
)


def usage(metrics, healthy=True) -> PlatformUsageData:
    return PlatformUsageData(
        platform=PlatformType.MINIMAX_CN,
        instance_id="x",
        display_name="x",
        metrics=metrics,
        is_healthy=healthy,
    )


# ------------------------------------------------------------ 状态色(顺序不可变)

def test_color_order():
    assert status_color(None) == COLOR_GRAY            # 无数据 → 灰
    assert status_color(usage([])) == COLOR_GRAY       # 空 metrics → 灰(不谎报红)
    assert status_color(usage([UsageMetric(LABEL_FIVE_HOUR, 96)], healthy=False)) == COLOR_RED  # 不健康优先
    assert status_color(usage([UsageMetric(LABEL_FIVE_HOUR, 5, 100)])) == COLOR_RED        # <10% 红
    assert status_color(usage([UsageMetric(LABEL_FIVE_HOUR, 30, 100)])) == COLOR_YELLOW    # <50% 黄
    assert status_color(usage([UsageMetric(LABEL_FIVE_HOUR, 80, 100)])) == COLOR_GREEN     # 否则绿
    # 余额型(无 total)看到期:<3 天红、<7 天黄
    assert status_color(usage([UsageMetric(LABEL_BALANCE, 500, None, "CNY", time.time() + 86400)])) == COLOR_RED
    assert status_color(usage([UsageMetric(LABEL_BALANCE, 500, None, "CNY", time.time() + 5 * 86400)])) == COLOR_YELLOW
    assert status_color(usage([UsageMetric(LABEL_BALANCE, 500, None, "CNY", time.time() + 30 * 86400)])) == COLOR_GREEN


# ------------------------------------------------------------ 托盘文本

def test_tray_text_modes():
    m = UsageMetric(LABEL_FIVE_HOUR, 80, 100, "%")
    assert tray_text(m, "remaining") == "80"
    assert tray_text(m, "used") == "19"  # 浮点上 (100-80)/100*100=19.999…,Swift Int() 同样截断为 19
    # 截断而非四舍五入(对齐 Swift Int()):66.666 → "66" 而非 "67"
    m2 = UsageMetric(LABEL_FIVE_HOUR, 66.666, 100, "%")
    assert tray_text(m2, "remaining") == "66"
    assert tray_text(m2, "used") == "33"
    assert tray_text(None, "remaining") == "--"
    assert tray_text(UsageMetric(LABEL_WEEKLY_UNLIMITED, 0, None, "unlimited"), "used") == "∞"


def test_tray_text_balance():
    assert tray_text(UsageMetric(LABEL_BALANCE, 253.7, None, "CNY"), "used") == "254"
    assert tray_text(UsageMetric(LABEL_BALANCE, 1250.0, None, "CNY"), "used") == "1.2K"


# ------------------------------------------------------------ 指标过滤(同族 + 回退)

def test_filter_exact_and_family():
    metrics = [
        UsageMetric(LABEL_FIVE_HOUR, 80, 100),
        UsageMetric(LABEL_WEEKLY_BOOSTED, 95, 100),  # 套餐当前是加成态
    ]
    # 勾了 weekly_limit(族名),加成态能命中
    got = filter_metrics(metrics, [LABEL_WEEKLY_LIMIT])
    assert [m.label for m in got] == [LABEL_WEEKLY_BOOSTED]
    # 勾两个:顺序按勾选顺序
    got = filter_metrics(metrics, [LABEL_WEEKLY_LIMIT, LABEL_FIVE_HOUR])
    assert [m.label for m in got] == [LABEL_WEEKLY_BOOSTED, LABEL_FIVE_HOUR]


def test_filter_fallback_to_first_two():
    """勾选与产出全无交集 → 回退前 2 个,状态栏不恒显 --"""
    metrics = [
        UsageMetric(LABEL_FIVE_HOUR, 80, 100),
        UsageMetric(LABEL_WEEKLY_UNLIMITED, 0, None, "unlimited"),
    ]
    got = filter_metrics(metrics, [LABEL_BALANCE])  # balance 不在产出里
    assert len(got) == 2


# ------------------------------------------------------------ 格式化

def test_format_credits_count():
    """对齐 Swift formatCredits:Int 截断 + 缩写两位小数"""
    assert format_credits_count(932, "zh-Hans") == "932"
    assert format_credits_count(8000000000, "zh-Hans") == "80.00亿"
    assert format_credits_count(32851959146, "zh-Hans") == "328.52亿"  # 与 Swift 文档同款示例同款结果
    assert format_credits_count(7750935720, "zh-Hans") == "77.51亿"
    assert format_credits_count(12500, "zh-Hans") == "1.25万"
    assert format_credits_count(12500, "en") == "12.50K"
    assert format_credits_count(2500000, "en") == "2.50M"
    assert format_credits_count(32851959146, "en") == "32851.96M"


def test_metric_detail_text():
    assert metric_detail_text(UsageMetric(LABEL_FIVE_HOUR, 80, 100), "zh-Hans") == "80%"
    assert metric_detail_text(UsageMetric(LABEL_FIVE_HOUR, 66.666, 100), "zh-Hans") == "66%"  # 截断
    assert metric_detail_text(UsageMetric("mcp_monthly", 932, 1000, "times"), "zh-Hans") == "932/1000"
    assert metric_detail_text(UsageMetric("mcp_monthly", 932, 8000000000, "times"), "zh-Hans") == "932/80.00亿"
    assert metric_detail_text(UsageMetric(LABEL_BALANCE, 253.7, None, "CNY"), "en") == "254"


def test_format_balance():
    assert format_balance(999.6) == "1000"
    assert format_balance(1250) == "1.2K"


def test_filter_same_family_no_duplicate():
    """勾了两个同族 label(周额度+加成),同一个 metric 只显示一次(对齐 Swift 族展开去重)"""
    metrics = [
        UsageMetric(LABEL_FIVE_HOUR, 80, 100),
        UsageMetric(LABEL_WEEKLY_BOOSTED, 95, 100),
    ]
    got = filter_metrics(metrics, [LABEL_WEEKLY_LIMIT, LABEL_WEEKLY_BOOSTED])
    assert [m.label for m in got] == [LABEL_WEEKLY_BOOSTED]
