# -*- coding: utf-8 -*-
"""系统浏览器登录 + CDP 提取 cookie。

为什么不用内嵌浏览器:PyQt5 5.15 自带的 Chromium 是 87 版(2021 年),
现代站点(Next.js 等)会直接报客户端异常、渲染不出来。

这里改用系统里最新的 Edge/Chrome:
1. 以 --remote-debugging-port 启动一个**独立用户目录**的浏览器实例(不动用户日常配置);
2. 用户在新窗口里正常登录(含验证码/滑块,不做任何自动化);
3. 程序通过 Chrome DevTools Protocol 读取该页面的全部 cookie,按平台契约取凭据;
4. 关闭这个临时浏览器实例。

CDP 走的是本地 HTTP + WebSocket,只连本机回环地址。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Optional

from ..models import PlatformType

#: 浏览器可执行文件的候选位置(优先 Edge)。
#: %LOCALAPPDATA% 是用户级安装(没有管理员权限时的常见位置),同样要找。
_BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _user_level_candidates() -> list[str]:
    return [
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            r"Google\Chrome\Application\chrome.exe",
        ),
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            r"Microsoft\Edge\Application\msedge.exe",
        ),
    ]


def find_browser() -> Optional[str]:
    """找系统里的 Edge/Chrome(系统级 + 用户级安装)"""
    for path in _BROWSER_CANDIDATES + _user_level_candidates():
        if path and os.path.isfile(path):
            return path
    return None

#: 各平台的登录页与凭据契约
LOGIN_URLS = {
    PlatformType.STEPFUN: "https://platform.stepfun.com/",
    PlatformType.TOKENRHYTHM: "https://tokenrhythm.studio/login",
}

#: 平台域名(用于在 CDP 目标列表里找对页面)
_PLATFORM_HOSTS = {
    PlatformType.STEPFUN: "stepfun.com",
    PlatformType.TOKENRHYTHM: "tokenrhythm.studio",
}


class BrowserLoginSession:
    """一次系统浏览器登录会话:启动 → 等待用户登录 → 取 cookie → 关闭。"""

    def __init__(self, platform: PlatformType):
        self._platform = platform
        self._process: Optional[subprocess.Popen] = None
        self._profile_dir = tempfile.mkdtemp(prefix="quotabar-login-")
        self._port: Optional[int] = None

    # ------------------------------------------------------------ 启动/关闭

    def start(self) -> bool:
        """启动独立实例的浏览器并打开登录页"""
        browser = find_browser()
        if browser is None:
            return False
        url = LOGIN_URLS.get(self._platform, "about:blank")
        args = [
            browser,
            f"--remote-debugging-port=0",
            f"--user-data-dir={self._profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            url,
        ]
        try:
            self._process = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError:
            # 浏览器存在但起不来(被安全软件拦截/权限不足):返回 False 让
            # 调用方给「启动失败」提示,不能让裸异常从调用链里冒出去
            return False
        self._port = self._wait_for_port()
        return self._port is not None

    def _wait_for_port(self, timeout: float = 20.0) -> Optional[int]:
        """浏览器会把实际端口写进 DevToolsActivePort 文件"""
        port_file = os.path.join(self._profile_dir, "DevToolsActivePort")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.isfile(port_file):
                try:
                    with open(port_file, encoding="utf-8") as f:
                        first = f.readline().strip()
                    if first.isdigit():
                        return int(first)
                except OSError:
                    pass
            time.sleep(0.25)
        return None

    def close(self) -> None:
        """关掉临时浏览器实例并清理用户目录

        清理要等浏览器**真的退出**再删目录:Windows 上 TerminateProcess 是异步的,
        进程垂死时 profile 里的文件还锁着,rmtree 会静默失败——目录(含已登录会话:
        Oasis-Token 30 天 refresh JWT / tr_session 30 天)就留在 %TEMP% 里了。
        """
        if self._process is not None:
            try:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=5)  # 等进程真正结束,否则文件还锁着
            except Exception:
                pass
            self._process = None
        # 文件锁可能刚放开,重试几次再放弃
        for _ in range(3):
            try:
                shutil.rmtree(self._profile_dir, ignore_errors=False)
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.3)
        # 删不掉就记一笔:目录里留着已登录的会话凭据,用户该知道。
        # 窗口化打包(PyInstaller windowed)下 stderr 无人可见,所以同时落一份
        # 到数据目录的 error.log——用户能看到残留路径,手动清理有据可依。
        if os.path.isdir(self._profile_dir):
            message = (
                f"[QuotaBar] 浏览器临时目录未能清理(可能仍被浏览器占用):{self._profile_dir}"
            )
            print(message, file=sys.stderr)
            try:
                from ..config import default_home

                log_path = os.path.join(default_home(), "error.log")
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
            except Exception:
                pass

    # ------------------------------------------------------------ 取 cookie

    def credential(self) -> Optional[str]:
        """按平台契约从浏览器里取凭据"""
        if self._port is None:
            return None
        cookies = self._fetch_cookies()
        if not cookies:
            return None
        jar = {c.get("name", ""): c.get("value", "") for c in cookies}
        if self._platform == PlatformType.STEPFUN:
            webid = jar.get("Oasis-Webid")
            token = jar.get("Oasis-Token")
            if webid and token:
                return f"Oasis-Webid={webid}; Oasis-Token={token}"
            return None
        if self._platform == PlatformType.TOKENRHYTHM:
            return jar.get("tr_session")
        return None

    def _fetch_cookies(self) -> list[dict]:
        """通过 CDP 拿目标页面的全部 cookie"""
        ws_url = self._find_page_ws()
        if ws_url is None:
            return []
        try:
            import websocket
        except ImportError:
            return []
        try:
            ws = websocket.create_connection(ws_url, timeout=15)
        except Exception:
            return []
        try:
            ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    msg = ws.recv()
                except Exception:
                    break
                if not isinstance(msg, str):
                    continue
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and data.get("id") == 1:
                    return (data.get("result") or {}).get("cookies") or []
        finally:
            try:
                ws.close()
            except Exception:
                pass
        return []

    def _find_page_ws(self) -> Optional[str]:
        """在 CDP 目标列表里找该平台的页面,拿它的调试地址"""
        url = f"http://127.0.0.1:{self._port}/json/list"
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        host = _PLATFORM_HOSTS.get(self._platform, "")
        fallback = None
        for t in targets:
            if t.get("type") != "page":
                continue
            ws = t.get("webSocketDebuggerUrl")
            if not ws:
                continue
            if host and host in (t.get("url") or ""):
                return ws
            fallback = fallback or ws
        return fallback
