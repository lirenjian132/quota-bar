# -*- coding: utf-8 -*-
"""Stepfun 会话自动续期(移植自 macOS 版 Services/Login/StepFunSessionRefresher.swift)。

契约(2026-09 实测,勿再探测生产端点):
  POST https://platform.stepfun.com/passport/proto.api.passport.v1.PassportService/RefreshToken
  头: oasis-appid: 10300 / oasis-platform: web / oasis-webid: 凭据里的 webid
   Cookie: 整串现有凭据 ("Oasis-Webid=x; Oasis-Token=<access>...<refresh>")
   body: "{}"
  200: {"accessToken":{"raw":...,"duration":1800,"mode":2},"refreshToken":{"raw":...}}
  mode=2 是有效会话;其它值视为失效。

预刷新策略:access TTL 约 1800 秒,本地解析 access JWT 的 exp,
剩余不到 300 秒才发刷新请求(纯本地判断,无请求成本)。解析不出 exp 就不刷新,
让主请求自然 401 走既有「重新登录」错误链。
"""

from __future__ import annotations

import base64
import json
import math
import time
from dataclasses import dataclass
from typing import Optional

from ..net import NetworkFunc, Request

REFRESH_URL = (
    "https://platform.stepfun.com/passport/"
    "proto.api.passport.v1.PassportService/RefreshToken"
)
#: access token 剩余寿命低于该值才预刷新(覆盖一次请求周期)
REFRESH_THRESHOLD = 300.0
APP_ID = "10300"
VALID_MODE = 2


@dataclass
class ParsedCredential:
    """从整串 cookie 里解析出的续期所需信息"""

    webid: str
    access_expiry: Optional[float]


def _cookie_pairs(credential: str):
    for pair in credential.split(";"):
        name, sep, value = pair.strip().partition("=")
        if sep:
            yield name.strip().lower(), value.strip()


def _decode_jwt_payload(jwt: str) -> Optional[dict]:
    """解 JWT 的 payload 段;坏 base64 / 坏 JSON / 非对象一律返回 None"""
    parts = jwt.split(".")
    if len(parts) < 2:
        return None
    raw = parts[1].replace("-", "+").replace("_", "/")
    raw += "=" * (-len(raw) % 4)
    try:
        data = json.loads(base64.b64decode(raw))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _expiry_from_payload(payload: dict) -> Optional[float]:
    """exp 是 Unix 秒;兼容数字与数字字符串(契约漂移),非有限值视为缺失"""
    value = payload.get("exp")
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def parse_credential(credential: str) -> Optional[ParsedCredential]:
    """解析 "Oasis-Webid=x; Oasis-Token=<access>...<refresh>"。

    缺 webid / token 不是双 JWT / access payload 解不出 JSON -> None。
    payload 合法但没有 exp -> 返回 expiry=None(视为无需刷新)。
    """
    if not isinstance(credential, str) or not credential:
        return None
    webid = ""
    token = ""
    for name, value in _cookie_pairs(credential):
        if name == "oasis-webid":
            webid = value
        elif name == "oasis-token":
            token = value
    if not webid or not token:
        return None
    parts = token.split("...")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    payload = _decode_jwt_payload(parts[0])
    if payload is None:
        return None
    return ParsedCredential(webid=webid, access_expiry=_expiry_from_payload(payload))


def needs_refresh(parsed: Optional[ParsedCredential], now: Optional[float] = None) -> bool:
    """access 剩余寿命 < 阈值才需要刷新;缺 exp 不刷新(交给主请求 401)"""
    if parsed is None or parsed.access_expiry is None:
        return False
    now = time.time() if now is None else now
    return parsed.access_expiry - now < REFRESH_THRESHOLD


def _is_jwt(value) -> bool:
    return isinstance(value, str) and len(value.split(".")) >= 3


def refresh_credential(
    credential: str,
    network: NetworkFunc,
    parsed: Optional[ParsedCredential] = None,
) -> Optional[str]:
    """执行一次 RefreshToken;成功返回新凭据串,任何异常/契约不符返回 None。

    调用方负责 CAS 写回(刷新在途时用户可能已经保存了新凭据)。
    """
    parsed = parsed or parse_credential(credential)
    if parsed is None:
        return None
    try:
        resp = network(
            Request(
                method="POST",
                url=REFRESH_URL,
                headers={
                    "Content-Type": "application/json",
                    "oasis-appid": APP_ID,
                    "oasis-platform": "web",
                    "oasis-webid": parsed.webid,
                    "Cookie": credential,
                },
                timeout=10.0,
                body="{}",
            )
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        body = json.loads(resp.text)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    access = body.get("accessToken")
    refresh = body.get("refreshToken")
    if not isinstance(access, dict) or not isinstance(refresh, dict):
        return None
    raw_access = access.get("raw")
    raw_refresh = refresh.get("raw")
    if access.get("mode") != VALID_MODE:
        return None
    if not _is_jwt(raw_access) or not _is_jwt(raw_refresh):
        return None
    return f"Oasis-Webid={parsed.webid}; Oasis-Token={raw_access}...{raw_refresh}"
