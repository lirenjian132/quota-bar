# -*- coding: utf-8 -*-
"""国际化:复用 macOS 版的 Resources/en.json 与 zh-Hans.json(已拷入 windows/Resources)。

占位符语义与 Swift 的 String(format:) 对齐:%@ 与 %d 按出现顺序消费参数。
未命中的 key 返回 key 本身(调用方可回退原始 label)。
"""

from __future__ import annotations

import json
import threading
from typing import Optional

LOCALE_EN = "en"
LOCALE_ZH = "zh-Hans"


def _default_resources_dir() -> str:
    import os

    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Resources")


class I18n:
    def __init__(self, resources_dir: Optional[str] = None, locale: str = LOCALE_ZH):
        self._dir = resources_dir or _default_resources_dir()
        self._tables: dict[str, dict[str, str]] = {}
        self._locale = locale
        self._lock = threading.Lock()
        for loc in (LOCALE_EN, LOCALE_ZH):
            path = f"{self._dir}/{loc}.json"
            try:
                with open(path, encoding="utf-8") as f:
                    table = json.load(f)
            except (OSError, json.JSONDecodeError):
                self._tables[loc] = {}
                continue
            # 资源表结构校验:根必须是 dict 且所有值都是 str。
            # 被杀软改写/打包漏文件时,坏表会让 t() 抛 AttributeError 或把
            # 非字符串塞进 QLabel(QLabel(123) 会被当成父窗口参数)。
            if not isinstance(table, dict):
                self._tables[loc] = {}
                continue
            self._tables[loc] = {
                str(key): value for key, value in table.items() if isinstance(key, str) and isinstance(value, str)
            }

    @property
    def locale(self) -> str:
        return self._locale

    def set_locale(self, locale: str) -> None:
        """切换语言:只认已知语言,其它值回落默认。

        非法值(list/dict 之类)会让下面查表时抛 TypeError(不可哈希)——
        整个界面一份文案都取不出来。
        """
        with self._lock:
            self._locale = locale if isinstance(locale, str) and locale in self._tables else LOCALE_ZH

    def t(self, key: str, *args) -> str:
        """按当前语言取文案;按出现顺序用 args 填充 %@ / %d 占位符"""
        table = {}
        if isinstance(self._locale, str):
            table = self._tables.get(self._locale) or {}
        s = table.get(key) or self._tables.get(LOCALE_EN, {}).get(key) or key
        if not args:
            return s
        out: list[str] = []
        arg_i = 0
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == "%":
                if i + 1 < len(s) and s[i + 1] == "%":
                    out.append("%")  # %% 转义为字面 %
                    i += 2
                    continue
                if i + 1 < len(s) and s[i + 1] in "@d" and arg_i < len(args):
                    out.append(str(args[arg_i]))
                    arg_i += 1
                    i += 2
                    continue
            out.append(ch)
            i += 1
        return "".join(out)


_instance: Optional[I18n] = None


def get_i18n() -> I18n:
    """模块级单例(UI 层换语言时直接 set_locale 即时生效)"""
    global _instance
    if _instance is None:
        _instance = I18n()
    return _instance
