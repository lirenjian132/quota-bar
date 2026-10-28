# -*- coding: utf-8 -*-
"""i18n 测试:占位符替换、回退行为。"""

import pytest

from quotabar.i18n import I18n, LOCALE_EN, LOCALE_ZH


@pytest.fixture
def i18n():
    return I18n()


def test_placeholder_substitution(i18n):
    i18n.set_locale(LOCALE_ZH)
    # %@ 与 %d 按出现顺序消费参数
    assert i18n.t("daily.reset.remaining", 2, 30) == "还剩2小时30分"
    assert i18n.t("menu.about.version", "2.2.0") .startswith("版本 2.2.0")


def test_missing_key_returns_key(i18n):
    assert i18n.t("no.such.key") == "no.such.key"
    assert i18n.t("no.such.key", "arg") == "no.such.key"  # 无占位符不动


def test_locale_switch(i18n):
    i18n.set_locale(LOCALE_EN)
    assert i18n.t("menu.refreshNow") == "Refresh Now"
    i18n.set_locale(LOCALE_ZH)
    assert i18n.t("menu.refreshNow") == "立即刷新"


def test_more_placeholders_than_args_safe(i18n):
    # 参数不足时剩余占位符原样保留,不抛错
    out = i18n.t("daily.reset.remaining", 2)
    assert "2" in out
