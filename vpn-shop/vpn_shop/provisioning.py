from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .config import Settings
from .security import days_from_now, normalize_username, now_ts, random_alias, random_subscription_id, to_xui_ms
from .store import Store
from .xui_api import XuiApiClient
from .xui_db import XuiDatabase


TEST_PROFILE_NOTES = "admin_test_24h_auto_delete"
ADMIN_PROFILE_NOTES = "admin_personal_long_lived"
PUBLIC_TRIAL_PROFILE_NOTES = "public_trial_7d_auto_delete"


class Provisioner:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.xui_api = XuiApiClient(
            panel_url=settings.xui_panel_url,
            username=settings.xui_username,
            password=settings.xui_password,
            verify_tls=settings.xui_verify_tls,
        )
        self.xui_db = XuiDatabase(settings.xui_db_path)

    def _transport_inbound_id(self, transport: str) -> int:
        if transport == "hybrid":
            raise ValueError("Hybrid transport must provision tcp and xhttp separately")
        if transport == "xhttp":
            return self.settings.xui_xhttp_inbound_id
        if transport == "tcp":
            return self.settings.xui_tcp_inbound_id
        raise ValueError(f"Unsupported transport: {transport}")

    def _infer_flow(self, inbound: dict[str, Any]) -> str:
        raw_settings = inbound.get("settings") or ""
        try:
            parsed = json.loads(raw_settings) if isinstance(raw_settings, str) else raw_settings
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            for client in parsed.get("clients") or []:
                flow = client.get("flow")
                if isinstance(flow, str) and flow:
                    return flow
        return ""

    def _build_client(self, inbound: dict[str, Any], *, alias: str, expires_at_s: int, device_limit: int = 0) -> dict[str, Any]:
        protocol = str(inbound.get("protocol") or "")
        client: dict[str, Any] = {
            "enable": True,
            "email": alias,
            "limitIp": max(int(device_limit), 0),
            "totalGB": 0,
            "expiryTime": to_xui_ms(expires_at_s),
            "tgId": 0,
            "subId": random_subscription_id(),
            "reset": 0,
            "comment": "",
        }

        if protocol in {"vless", "vmess"}:
            client["id"] = str(uuid.uuid4())
            flow = self._infer_flow(inbound)
            if flow:
                client["flow"] = flow
        elif protocol == "trojan":
            client["password"] = str(uuid.uuid4())
        elif protocol == "shadowsocks":
            client["password"] = str(uuid.uuid4())
        else:
            raise ValueError(f"Unsupported inbound protocol for provisioning: {protocol}")

        return client

    def _provision_profile(
        self,
        *,
        transport: str,
        duration_days: int,
        profile_mode: str,
        family_label: str | None,
        alias: str | None = None,
        notes: str = "",
        device_limit: int = 0,
    ) -> dict[str, Any]:
        inbound_id = self._transport_inbound_id(transport)
        expires_at_s = days_from_now(int(duration_days))
        alias = alias or family_label or random_alias()
        inbound = self.xui_api.get_inbound(inbound_id)
        client = self._build_client(inbound, alias=alias, expires_at_s=expires_at_s, device_limit=device_limit)
        self.xui_api.add_client(inbound_id, client)

        found = None
        for _ in range(10):
            found = self.xui_db.find_client_by_email(alias)
            if found:
                break
            time.sleep(0.2)
        if not found:
            raise RuntimeError(f"Client {alias} was not found in x-ui.db after provisioning")

        sub_id = found["client"].get("subId")
        if not sub_id:
            raise RuntimeError(f"Client {alias} has no subId in x-ui.db")

        profile = self.store.create_profile(
            xui_inbound_id=inbound_id,
            transport=transport,
            profile_mode=profile_mode,
            family_label=family_label,
            xui_email=alias,
            xui_client_id=found["client"].get("id") or found["client"].get("password") or alias,
            expires_at=expires_at_s,
            notes=notes,
        )

        return {
            "profile": profile,
            "subscription_url": f"{self.settings.subscription_base_url}/{sub_id}",
            "xui_email": alias,
            "sub_id": sub_id,
            "expires_at": expires_at_s,
        }

    def create_profile_for_order(self, order: dict[str, Any]) -> dict[str, Any]:
        order_meta = order.get("meta_json") or {}
        raw_device_limit = order_meta.get("device_limit", self.settings.default_device_limit)
        device_limit = int(self.settings.default_device_limit if raw_device_limit is None else raw_device_limit)
        if str(order["transport"]) == "hybrid":
            return self.create_hybrid_profile_for_order(order, device_limit=device_limit)
        result = self._provision_profile(
            transport=order["transport"],
            duration_days=int(order["duration_days"]),
            profile_mode=order["profile_mode"],
            family_label=order["family_label"],
            device_limit=device_limit,
        )
        self.store.link_order_profile(order["public_id"], result["profile"]["public_id"])
        return result

    def create_hybrid_profile_for_order(self, order: dict[str, Any], *, device_limit: int) -> dict[str, Any]:
        alias_prefix = random_alias(prefix="hybrid", size=6)
        tcp_result: dict[str, Any] | None = None
        try:
            tcp_result = self._provision_profile(
                transport="tcp",
                duration_days=int(order["duration_days"]),
                profile_mode=str(order["profile_mode"]),
                family_label=None,
                alias=f"{alias_prefix}-tcp",
                device_limit=device_limit,
            )
            xhttp_result = self._provision_profile(
                transport="xhttp",
                duration_days=int(order["duration_days"]),
                profile_mode=str(order["profile_mode"]),
                family_label=None,
                alias=f"{alias_prefix}-xhttp",
                device_limit=device_limit,
            )
        except Exception:
            if tcp_result:
                profile = tcp_result.get("profile") or {}
                delete_keys = [str(tcp_result.get("xui_email") or "")]
                client_id = str(profile.get("xui_client_id") or "")
                if client_id:
                    delete_keys.append(client_id)
                for client_key in [key for key in delete_keys if key]:
                    try:
                        self.xui_api.delete_client(int(profile["xui_inbound_id"]), client_key)
                        break
                    except Exception:
                        pass
                if profile.get("public_id"):
                    self.store.mark_profile_deleted(str(profile["public_id"]))
            raise

        self.store.link_order_profile(order["public_id"], tcp_result["profile"]["public_id"])
        meta = dict(order.get("meta_json") or {})
        meta.update(
            {
                "hybrid": True,
                "tcp_profile_public_id": tcp_result["profile"]["public_id"],
                "xhttp_profile_public_id": xhttp_result["profile"]["public_id"],
                "tcp_sub_id": tcp_result["sub_id"],
                "xhttp_sub_id": xhttp_result["sub_id"],
                "xhttp_profile_public_id_for_cleanup": xhttp_result["profile"]["public_id"],
            }
        )
        self.store.update_order_meta(order["public_id"], meta)
        return {
            "profile": tcp_result["profile"],
            "xhttp_profile": xhttp_result["profile"],
            "subscription_url": "",
            "tcp_sub_id": tcp_result["sub_id"],
            "xhttp_sub_id": xhttp_result["sub_id"],
            "expires_at": min(int(tcp_result["expires_at"]), int(xhttp_result["expires_at"])),
        }

    def create_test_profile(self, transport: str) -> dict[str, Any]:
        return self._provision_profile(
            transport=transport,
            duration_days=1,
            profile_mode="anonymous",
            family_label=None,
            notes=TEST_PROFILE_NOTES,
        )

    def create_hybrid_test_profile(self) -> dict[str, Any]:
        alias_prefix = random_alias(prefix="hybrid-test", size=6)
        tcp_result: dict[str, Any] | None = None
        try:
            tcp_result = self._provision_profile(
                transport="tcp",
                duration_days=1,
                profile_mode="anonymous",
                family_label=None,
                alias=f"{alias_prefix}-tcp",
                notes=TEST_PROFILE_NOTES,
            )
            xhttp_result = self._provision_profile(
                transport="xhttp",
                duration_days=1,
                profile_mode="anonymous",
                family_label=None,
                alias=f"{alias_prefix}-xhttp",
                notes=TEST_PROFILE_NOTES,
            )
        except Exception:
            if tcp_result:
                profile = tcp_result.get("profile") or {}
                delete_keys = [str(tcp_result.get("xui_email") or "")]
                client_id = str(profile.get("xui_client_id") or "")
                if client_id:
                    delete_keys.append(client_id)
                for client_key in [key for key in delete_keys if key]:
                    try:
                        self.xui_api.delete_client(int(profile["xui_inbound_id"]), client_key)
                        break
                    except Exception:
                        pass
                if profile.get("public_id"):
                    self.store.mark_profile_deleted(str(profile["public_id"]))
            raise

        return {
            "tcp": tcp_result,
            "xhttp": xhttp_result,
            "tcp_sub_id": tcp_result["sub_id"],
            "xhttp_sub_id": xhttp_result["sub_id"],
            "expires_at": min(int(tcp_result["expires_at"]), int(xhttp_result["expires_at"])),
        }

    def renew_profile(self, profile_public_id: str, duration_days: int, device_limit: int | None = None) -> dict[str, Any]:
        profile = self.store.get_profile(profile_public_id)
        if not profile or profile.get("status") == "deleted":
            raise RuntimeError(f"Profile {profile_public_id} is not renewable")

        found = self.xui_db.find_client_by_email(str(profile["xui_email"]))
        if not found:
            raise RuntimeError(f"Profile {profile_public_id} is absent in x-ui.db")

        client = dict(found["client"])
        client_expiry_ms = int(client.get("expiryTime") or 0)
        client_expiry_s = client_expiry_ms // 1000 if client_expiry_ms > 0 else 0
        base_expires_at = max(now_ts(), int(profile.get("expires_at") or 0), client_expiry_s)
        new_expires_at = base_expires_at + int(duration_days) * 24 * 3600
        client["expiryTime"] = to_xui_ms(new_expires_at)
        client["enable"] = True
        if device_limit is not None:
            client["limitIp"] = max(int(device_limit), 0)

        client_key = str(client.get("id") or client.get("password") or profile.get("xui_client_id") or "")
        if not client_key:
            raise RuntimeError(f"Profile {profile_public_id} has no x-ui client key")

        inbound_id = int(found.get("inbound_id") or profile["xui_inbound_id"])
        self.xui_api.update_client(inbound_id, client_key, client)
        updated_profile = self.store.extend_profile(profile_public_id, new_expires_at)
        sub_id = str(client.get("subId") or "")
        if not sub_id:
            raise RuntimeError(f"Profile {profile_public_id} has no subId in x-ui.db")
        return {
            "profile": updated_profile,
            "subscription_url": f"{self.settings.subscription_base_url}/{sub_id}",
            "xui_email": profile["xui_email"],
            "sub_id": sub_id,
            "expires_at": new_expires_at,
        }

    def set_profile_enabled(self, profile_public_id: str, enabled: bool) -> dict[str, Any]:
        profile = self.store.get_profile(profile_public_id)
        if not profile or profile.get("status") == "deleted":
            raise RuntimeError(f"Profile {profile_public_id} is absent or deleted")

        found = self.xui_db.find_client_by_email(str(profile["xui_email"]))
        if not found:
            raise RuntimeError(f"Profile {profile_public_id} is absent in x-ui.db")

        client = dict(found["client"])
        client["enable"] = bool(enabled)
        client_key = str(client.get("id") or client.get("password") or profile.get("xui_client_id") or "")
        if not client_key:
            raise RuntimeError(f"Profile {profile_public_id} has no x-ui client key")

        inbound_id = int(found.get("inbound_id") or profile["xui_inbound_id"])
        self.xui_api.update_client(inbound_id, client_key, client)

        return {
            "profile": profile,
            "xui_email": profile["xui_email"],
            "inbound_id": inbound_id,
            "enabled": bool(enabled),
        }

    def create_public_trial_profile(self, user: dict[str, Any], transport: str = "tcp") -> dict[str, Any]:
        username = normalize_username(user.get("username"))
        user_id = int(user.get("id") or 0)
        alias_prefix = f"trial-{username}" if username else f"trial-id{user_id}"
        return self._provision_profile(
            transport=transport,
            duration_days=7,
            profile_mode="anonymous",
            family_label=None,
            alias=random_alias(prefix=alias_prefix, size=6),
            notes=PUBLIC_TRIAL_PROFILE_NOTES,
            device_limit=self.settings.default_device_limit,
        )

    def create_admin_profile(self, transport: str, user: dict[str, Any]) -> dict[str, Any]:
        username = normalize_username(user.get("username"))
        user_id = int(user.get("id") or 0)
        alias_prefix = f"admin-{username}" if username else f"admin-id{user_id}"
        family_label = random_alias(prefix=alias_prefix, size=6)
        return self._provision_profile(
            transport=transport,
            duration_days=36500,
            profile_mode="family",
            family_label=family_label,
            notes=ADMIN_PROFILE_NOTES,
        )
