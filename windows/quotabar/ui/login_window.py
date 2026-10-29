# -*- coding: utf-8 -*-
"""cookie 型平台的一键登录窗(Stepfun / 基元律动)。

对标 macOS 版的 WebLoginRenewal:内嵌浏览器打开官网登录页,用户在网页里正常
登录(含验证码/滑块,不做任何自动化),点「完成并提取」后从浏览器 cookie 里
按平台契约取出凭据:

- Stepfun   : Oasis-Webid + Oasis-Token 两段,拼成整串 cookie
- 基元律动 : tr_session 的**裸值**(模板会再拼 "tr_session=" 前缀,弄反会重复)

浏览器用独立的非持久化 profile,登录态只活在本窗内,关窗即清。
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import QUrl, Qt
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..i18n import get_i18n
from ..models import PlatformType

#: QtWebEngine 必须在 QApplication 创建前导入,且需要 AA_ShareOpenGLContexts。
#: 这里做延迟导入,由调用方(run.py / 配置面板)在建 QApplication 前先 import 本模块。
_webengine_ready = False


def ensure_webengine() -> None:
    """确保 WebEngine 已导入(QApplication 创建前调用)"""
    global _webengine_ready
    if _webengine_ready:
        return
    from PyQt5.QtWebEngineWidgets import (
        QWebEnginePage,
        QWebEngineProfile,
        QWebEngineView,
    )

    # 放进模块命名空间,类体里可直接引用
    globals()["QWebEnginePage"] = QWebEnginePage
    globals()["QWebEngineProfile"] = QWebEngineProfile
    globals()["QWebEngineView"] = QWebEngineView
    _webengine_ready = True

#: 现代 Chrome UA(PyQt5 内置 Chromium 版本较老,需伪装)
_MODERN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: 各平台的登录页与凭据提取契约
_LOGIN_URLS = {
    PlatformType.STEPFUN: "https://platform.stepfun.com/",
    PlatformType.TOKENRHYTHM: "https://tokenrhythm.studio/login",
}


class LoginWindow(QDialog):
    """登录窗:加载官网登录页 → 用户登录 → 点「完成并提取」→ 回调交回凭据。"""

    def __init__(self, platform: PlatformType, parent=None):
        super().__init__(parent)
        ensure_webengine()
        self._platform = platform
        self._result: Optional[str] = None
        i18n = get_i18n()
        self.setWindowTitle(i18n.t("login.title", platform.display_name()))
        self.resize(920, 720)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._view = QWebEngineView(self)
        layout.addWidget(self._view, 1)

        bottom = QHBoxLayout()
        hint = QLabel(i18n.t("login.hint"))
        self._status = hint
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666; font-size:12px; padding:6px 8px;")
        bottom.addWidget(hint, 1)
        cancel = QPushButton(i18n.t("common.cancel"))
        cancel.clicked.connect(self.reject)
        bottom.addWidget(cancel)
        done = QPushButton(i18n.t("login.complete"))
        done.setDefault(True)
        done.clicked.connect(self._on_done)
        bottom.addWidget(done)
        layout.addLayout(bottom)

        # 独立非持久化 profile:登录态只活在本窗,关窗即清
        self._profile = QWebEngineProfile(self)
        self._profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)
        self._profile.setHttpCacheType(QWebEngineProfile.MemoryHttpCache)
        # PyQt5 5.15 自带的 Chromium 是 87 版(2021),站点的 UA 判断会走老 polyfill 分支
        # 而报 Next.js 客户端异常。伪装成现代 Chrome 让站点加载正确的资源包。
        self._profile.setHttpUserAgent(_MODERN_UA)
        # 页面的 profile 不能直接换(Qt5 不支持),改用 cookie 隔离:
        # 独立 profile 的 page 才有自己的 cookie 罐
        page = QWebEnginePage(self._profile, self)
        self._page = page  # 持有引用:profile 先于 page 析构会告警
        self._view.setPage(page)
        url = _LOGIN_URLS.get(platform)
        if url:
            self._view.load(QUrl(url))
        self._view.loadFinished.connect(self._on_load_finished)

    def _on_load_finished(self, ok: bool) -> None:
        # 站点脚本报错时给出明确提示(内置 Chromium 版本偏老时常见)
        if self._page_failed():
            self._status.setText(get_i18n().t("login.webengineTooOld"))
            self._status.setStyleSheet("color:#c0392b; font-size:12px; padding:6px 8px;")

    def _page_failed(self) -> bool:
        # 页面是否是站点的报错页(Application error 等)
        try:
            html = self._view.page().toHtml()
        except Exception:
            return False
        markers = ("Application error", "client-side exception", "Internal Server Error")
        return any(m in html for m in markers)

    def closeEvent(self, event) -> None:
        # 先销毁 page 再让 profile 析构,否则 Qt 会警告 profile 提前释放
        page = getattr(self, "_page", None)
        if page is not None:
            page.deleteLater()
            self._page = None
        super().closeEvent(event)

    def credential(self) -> Optional[str]:
        """提取到的凭据(平台约定的形态);没取到返回 None"""
        return self._result

    def _on_done(self) -> None:
        """从 cookie 里按平台契约提取凭据"""
        self._result = self._extract_cookie()
        if self._result:
            self.accept()
        else:
            i18n = get_i18n()
            box = QLabel(i18n.t("login.missing.message"))
            box.setWindowTitle(i18n.t("login.missing.title"))
            box.setStyleSheet("background:#fff; color:#c00; padding:10px;")
            box.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
            box.setAlignment(Qt.AlignCenter)
            box.resize(360, 90)
            box.show()
            self._missing_box = box

    def _extract_cookie(self) -> Optional[str]:
        """按平台契约从 cookie 罐里取凭据"""
        try:
            cookies = self._view.page().profile().cookieStore().cookieList()
        except Exception:
            return None
        jar = {}
        for c in cookies:
            try:
                name = bytes(c.name()).decode("utf-8", "ignore")
                value = bytes(c.value()).decode("utf-8", "ignore")
            except Exception:
                continue
            jar[name] = value

        if self._platform == PlatformType.STEPFUN:
            webid = jar.get("Oasis-Webid")
            token = jar.get("Oasis-Token")
            if webid and token:
                return f"Oasis-Webid={webid}; Oasis-Token={token}"
            return None
        if self._platform == PlatformType.TOKENRHYTHM:
            return jar.get("tr_session")
        return None
