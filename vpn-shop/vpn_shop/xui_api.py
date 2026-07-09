from __future__ import annotations

import http.cookiejar
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class XuiApiError(RuntimeError):
    pass


class XuiApiClient:
    def __init__(self, *, panel_url: str, username: str, password: str, verify_tls: bool) -> None:
        self.panel_url = panel_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.verify_tls = verify_tls
        self._logged_in = False
        self._cookie_jar = http.cookiejar.CookieJar()
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(self._cookie_jar)]
        if not verify_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    def _url(self, path: str) -> str:
        return urllib.parse.urljoin(self.panel_url, path.lstrip("/"))

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "vpn-shop/0.1",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = urllib.request.Request(self._url(path), data=data, headers=headers, method=method.upper())
        try:
            with self._opener.open(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise XuiApiError(f"HTTP {exc.code} for {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise XuiApiError(f"Request failed for {path}: {exc}") from exc

        if not raw:
            # 3x-ui is known to return an empty body for some successful addClient/updateClient calls.
            return {}

        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise XuiApiError(f"Invalid JSON response for {path}: {text}") from exc

    def login(self) -> None:
        if self._logged_in:
            return
        if not self.username or not self.password:
            raise XuiApiError("XUI_USERNAME and XUI_PASSWORD must be configured")
        response = self._request(
            "POST",
            "/login",
            {"username": self.username, "password": self.password},
        )
        if isinstance(response, dict) and response.get("success") is False:
            raise XuiApiError(response.get("msg") or "3x-ui login failed")
        self._logged_in = True

    @staticmethod
    def _should_retry_after_reauth(path: str, exc: XuiApiError) -> bool:
        message = str(exc)
        if not path.startswith("/panel/api/"):
            return False
        return any(token in message for token in ("HTTP 401", "HTTP 403", "HTTP 404"))

    def _reset_session(self) -> None:
        self._logged_in = False
        self._cookie_jar.clear()

    def _api(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        self.login()
        try:
            return self._request(method, path, payload)
        except XuiApiError as exc:
            if not self._logged_in or not self._should_retry_after_reauth(path, exc):
                raise
        self._reset_session()
        self.login()
        return self._request(method, path, payload)

    @staticmethod
    def _unwrap(response: Any) -> Any:
        if isinstance(response, dict) and "obj" in response:
            if response.get("success") is False:
                raise XuiApiError(response.get("msg") or "3x-ui API call failed")
            return response["obj"]
        return response

    def get_inbounds(self) -> list[dict[str, Any]]:
        response = self._api("GET", "/panel/api/inbounds/list")
        result = self._unwrap(response)
        if not isinstance(result, list):
            raise XuiApiError("Unexpected inbounds response")
        return result

    def get_inbound(self, inbound_id: int) -> dict[str, Any]:
        try:
            response = self._api("GET", f"/panel/api/inbounds/get/{inbound_id}")
            result = self._unwrap(response)
            if isinstance(result, dict):
                return result
        except XuiApiError:
            pass

        for inbound in self.get_inbounds():
            if int(inbound.get("id") or 0) == int(inbound_id):
                return inbound
        raise XuiApiError(f"Inbound {inbound_id} not found")

    def add_client(self, inbound_id: int, client: dict[str, Any]) -> None:
        self._api(
            "POST",
            "/panel/api/inbounds/addClient",
            {
                "id": inbound_id,
                "settings": json.dumps({"clients": [client]}, ensure_ascii=False),
            },
        )

    def update_client(self, inbound_id: int, client_key: str, client: dict[str, Any]) -> None:
        self._api(
            "POST",
            f"/panel/api/inbounds/updateClient/{urllib.parse.quote(client_key, safe='')}",
            {
                "id": inbound_id,
                "settings": json.dumps({"clients": [client]}, ensure_ascii=False),
            },
        )

    def delete_client(self, inbound_id: int, client_key: str) -> None:
        self._api(
            "POST",
            f"/panel/api/inbounds/{inbound_id}/delClient/{urllib.parse.quote(client_key, safe='')}",
        )

    def reset_client_traffic(self, inbound_id: int, email: str) -> None:
        self._api(
            "POST",
            f"/panel/api/inbounds/{inbound_id}/resetClientTraffic/{urllib.parse.quote(email, safe='')}",
        )

    def restart_xray(self) -> None:
        self._api("POST", "/panel/api/server/restartXrayService")
