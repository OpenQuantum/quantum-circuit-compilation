"""Explicit operator calls. No database sharing, sender impersonation or write retries."""

from urllib.parse import quote

import httpx
from star_bff.errors import BFFError

API = "/api/quantum/v1"
UPSTREAM = "/operator/v1"


def public_links(value):
    if isinstance(value, list):
        return [public_links(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: public_links(item) for key, item in value.items()}
    link = result.get("download_url")
    if isinstance(link, str) and link.startswith(UPSTREAM + "/artifacts/"):
        result["download_url"] = API + link[len(UPSTREAM):]
    return result


class OperatorClient:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url=settings.operator_url.rstrip("/"),
            timeout=httpx.Timeout(settings.request_timeout, connect=5),
            follow_redirects=False, trust_env=False, transport=transport,
        )

    def headers(self, user):
        token = self.settings.operator_token.get_secret_value()
        if not token:
            raise BFFError("WORLD_NOT_CONFIGURED", "尚未配置 Quantum World 操作者连接。", 503, layer="world")
        # Only the authenticated BFF session supplies this identity.
        return {"Authorization": "Bearer " + token, "X-Quantum-User": user["userId"]}

    @staticmethod
    def identifier(value):
        if not value or len(value) > 2048 or any(c in value for c in "/\\\x00") or value in {".", ".."}:
            raise BFFError("INVALID_IDENTIFIER", "资源标识无效。", 422)
        return quote(value, safe="")

    @staticmethod
    def check(response):
        if 200 <= response.status_code < 300:
            return
        try:
            payload = response.json()
            error = payload.get("error", payload.get("data", {}))
            code = str(error.get("code", "WORLD_ERROR"))[:100]
            message = str(error.get("message", "Quantum World 请求未完成。"))[:800]
        except (ValueError, AttributeError, TypeError):
            code, message = "WORLD_ERROR", "Quantum World 请求未完成。"
        status = response.status_code if 400 <= response.status_code < 600 else 502
        # Service credentials are configuration, not the browser user's login.
        if status == 401:
            code, message, status = "WORLD_AUTH_FAILED", "BFF 与 World 的服务凭据不匹配。", 502
        raise BFFError(code, message, status, layer="world")

    async def request(self, user, method, path, *, params=None, body=None, submission=False):
        try:
            response = await self.http.request(
                method, UPSTREAM + path, headers=self.headers(user), params=params, json=body,
                timeout=self.settings.submit_timeout if submission else self.settings.request_timeout,
            )
        except httpx.RequestError:
            if method != "GET":
                raise BFFError(
                    "WORLD_ACTION_UNCERTAIN", "未收到操作回执，请刷新状态后核对；系统不会自动重试。",
                    502, layer="world",
                ) from None
            raise BFFError("WORLD_UNAVAILABLE", "暂时无法连接 Quantum World。", 503, True, "world") from None
        self.check(response)
        try:
            result = response.json()
        except ValueError:
            raise BFFError("WORLD_INVALID_RESPONSE", "World 返回了无效结果，请刷新核对状态。", 502, layer="world") from None
        return public_links(result)

    async def download(self, user, artifact_id, headers, method="GET"):
        forwarded = self.headers(user)
        for name in ("range", "if-range"):
            if name in headers:
                if len(headers[name]) > 256:
                    raise BFFError("INVALID_RANGE", "下载范围无效。", 422)
                forwarded[name] = headers[name]
        request = self.http.build_request(
            method, UPSTREAM + "/artifacts/" + self.identifier(artifact_id), headers=forwarded,
        )
        try:
            response = await self.http.send(request, stream=True)
        except httpx.RequestError:
            raise BFFError("WORLD_UNAVAILABLE", "暂时无法读取 World 产物。", 503, True, "world") from None
        if response.status_code not in {200, 206, 304, 416}:
            try:
                await response.aread()
                self.check(response)
            finally:
                await response.aclose()
        return response

    async def close(self):
        await self.http.aclose()
