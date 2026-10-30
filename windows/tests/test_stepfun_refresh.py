# -*- coding: utf-8 -*-
"""Stepfun 会话自动续期测试:JWT exp 解析、刷新请求契约、CAS 写回。"""

import base64
import json
import time

from quotabar.config import ConfigService
from quotabar.manager import PlatformManager
from quotabar.models import PlatformType
from quotabar.net import Response
from quotabar.platforms.stepfun_refresh import (
    REFRESH_URL,
    needs_refresh,
    parse_credential,
    refresh_credential,
)


def _b64(obj) -> str:
    raw = json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwt(payload, signature="sig") -> str:
    return f"{_b64({'alg': 'none'})}.{_b64(payload)}.{signature}"


def _credential(exp: float, webid="web-1") -> str:
    access = _jwt({"exp": exp, "device_id": webid})
    refresh = _jwt({"exp": exp + 86400, "device_id": webid})
    return f"Oasis-Webid={webid}; Oasis-Token={access}...{refresh}"


def test_parse_credential_reads_exp_and_webid():
    exp = time.time() + 1800
    parsed = parse_credential(_credential(exp))
    assert parsed is not None
    assert parsed.webid == "web-1"
    assert abs(parsed.access_expiry - exp) < 1


def test_parse_credential_accepts_string_exp():
    exp = time.time() + 1800
    access = _jwt({"exp": str(int(exp))})
    refresh = _jwt({"exp": int(exp) + 100})
    parsed = parse_credential(f"Oasis-Webid=w; Oasis-Token={access}...{refresh}")
    assert parsed is not None
    assert parsed.access_expiry == int(exp)


def test_parse_credential_rejects_single_jwt():
    access = _jwt({"exp": time.time() + 100})
    assert parse_credential(f"Oasis-Webid=w; Oasis-Token={access}") is None


def test_needs_refresh_only_near_expiry():
    now = time.time()
    assert needs_refresh(parse_credential(_credential(now + 100)), now) is True
    assert needs_refresh(parse_credential(_credential(now + 3600)), now) is False
    # 没有 exp:不刷新,让主请求自然 401
    access = _jwt({"sub": "x"})
    refresh = _jwt({"exp": now + 100})
    parsed = parse_credential(f"Oasis-Webid=w; Oasis-Token={access}...{refresh}")
    assert needs_refresh(parsed, now) is False


def test_refresh_credential_success_and_request_contract():
    old = _credential(time.time() + 100)
    new_access = _jwt({"exp": time.time() + 1800})
    new_refresh = _jwt({"exp": time.time() + 86400})
    seen = {}

    def network(req):
        seen["req"] = req
        return Response(
            200,
            json.dumps(
                {
                    "accessToken": {"raw": new_access, "duration": 1800, "mode": 2},
                    "refreshToken": {"raw": new_refresh},
                }
            ),
        )

    fresh = refresh_credential(old, network)
    assert fresh is not None
    assert fresh.startswith("Oasis-Webid=web-1; Oasis-Token=")
    assert fresh.endswith(f"...{new_refresh}")
    req = seen["req"]
    assert req.url == REFRESH_URL
    assert req.method == "POST"
    assert req.body == "{}"
    assert req.headers["oasis-appid"] == "10300"
    assert req.headers["oasis-webid"] == "web-1"
    assert req.headers["Cookie"] == old


def test_refresh_credential_rejects_bad_responses():
    old = _credential(time.time() + 100)
    new_access = _jwt({"exp": time.time() + 1800})
    new_refresh = _jwt({"exp": time.time() + 86400})

    def resp(status, body):
        return Response(status, json.dumps(body))

    assert refresh_credential(old, lambda req: resp(401, {})) is None
    assert (
        refresh_credential(
            old,
            lambda req: resp(
                200,
                {
                    "accessToken": {"raw": new_access, "mode": 1},
                    "refreshToken": {"raw": new_refresh},
                },
            ),
        )
        is None
    )
    assert (
        refresh_credential(
            old,
            lambda req: resp(
                200,
                {
                    "accessToken": {"raw": "not-a-jwt", "mode": 2},
                    "refreshToken": {"raw": new_refresh},
                },
            ),
        )
        is None
    )
    assert refresh_credential(old, lambda req: resp(200, [])) is None


def _config_with_stepfun(tmp_path, credential):
    config = ConfigService(home=str(tmp_path / "home"))
    inst = config.add_instance(PlatformType.STEPFUN)
    config.set_api_key(inst.id, credential)
    return config, inst.id


def test_manager_refresh_writes_new_credential(tmp_path):
    old = _credential(time.time() + 100)
    config, iid = _config_with_stepfun(tmp_path, old)
    new_access = _jwt({"exp": time.time() + 1800})
    new_refresh = _jwt({"exp": time.time() + 86400})

    def network(req):
        return Response(
            200,
            json.dumps(
                {
                    "accessToken": {"raw": new_access, "mode": 2},
                    "refreshToken": {"raw": new_refresh},
                }
            ),
        )

    PlatformManager(config, network=network)._refresh_stepfun_if_needed(iid)
    saved = config.api_key(iid)
    assert saved != old
    assert new_access in saved and new_refresh in saved


def test_manager_refresh_does_not_overwrite_user_save(tmp_path):
    """刷新在途时用户保存了新凭据:CAS 放弃写回,用户的新值优先"""
    old = _credential(time.time() + 100)
    config, iid = _config_with_stepfun(tmp_path, old)
    new_access = _jwt({"exp": time.time() + 1800})
    new_refresh = _jwt({"exp": time.time() + 86400})

    def network(req):
        config.set_api_key(iid, "user-saved-new")
        return Response(
            200,
            json.dumps(
                {
                    "accessToken": {"raw": new_access, "mode": 2},
                    "refreshToken": {"raw": new_refresh},
                }
            ),
        )

    PlatformManager(config, network=network)._refresh_stepfun_if_needed(iid)
    assert config.api_key(iid) == "user-saved-new"
