from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import logging
from pathlib import Path
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from .catalog import Offer, build_offers
from .config import Settings, load_settings
from .provisioning import Provisioner
from .security import now_ts
from .store import Store
from .telegram_api import TelegramBotClient, TelegramApiError


LOGGER = logging.getLogger("vpn-shop-web")
PAYMENT_REPORT_REPEAT_SECONDS = 10 * 60
TEMP_NETWORK_NOTICE = ""


def money(value: int | str | None) -> str:
    return f"{int(value or 0)} RUB"


def transport_label(transport: str) -> str:
    if transport == "xhttp":
        return "XHTTP"
    if transport == "tcp":
        return "Стандартный"
    if transport == "hybrid":
        return "Универсальный"
    return transport


def device_limit_label(device_limit: int | str | None) -> str:
    limit = int(device_limit or 0)
    if limit <= 0:
        return "без лимита"
    return f"до {limit} устройств одновременно"


def public_origin(headers: Any, fallback: str) -> str:
    host = headers.get("X-Forwarded-Host") or headers.get("Host")
    if host:
        proto = headers.get("X-Forwarded-Proto") or "https"
        return f"{proto}://{host}".rstrip("/")
    return fallback.rstrip("/")


def subscription_import_url(subscription_url: str, target: str) -> str:
    parsed = urlsplit(subscription_url)
    parts = [segment for segment in parsed.path.split("/") if segment]
    if len(parts) >= 3:
        sub_id = parts[-1]
        base_parts = parts[:-2]
        import_path = "/" + "/".join([*base_parts, "import", target, quote(sub_id, safe="")])
    else:
        sub_id = parts[-1] if parts else ""
        import_path = f"/import/{target}/{quote(sub_id, safe='')}"
    query = urlencode({"url": subscription_url})
    return urlunsplit((parsed.scheme, parsed.netloc, import_path, query, ""))


def subscription_setup_url(subscription_url: str) -> str:
    parsed = urlsplit(subscription_url)
    parts = [segment for segment in parsed.path.split("/") if segment]
    if len(parts) >= 3:
        sub_id = parts[-1]
        base_parts = parts[:-2]
        import_path = "/" + "/".join([*base_parts, "import", quote(sub_id, safe="~")])
    else:
        sub_id = parts[-1] if parts else ""
        import_path = f"/import/{quote(sub_id, safe='~')}"
    query = urlencode({"url": subscription_url})
    return urlunsplit((parsed.scheme, parsed.netloc, import_path, query, ""))


def subscription_url_for_route(base_url: str, route: str, subscription_id: str) -> str:
    parsed = urlsplit(base_url)
    base_parts = [segment for segment in parsed.path.split("/") if segment]
    if base_parts:
        base_parts = base_parts[:-1]
    path = "/" + "/".join([*base_parts, route, quote(subscription_id, safe="~")])
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def inline_admin_markup(order_public_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Подтвердить оплату", "callback_data": f"admin:confirm:{order_public_id}", "style": "success"}],
            [{"text": "Отменить заказ", "callback_data": f"admin:cancel:{order_public_id}", "style": "danger"}],
        ]
    }


class WebCheckout:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.store.init()
        self.offers = build_offers(settings)
        self.provisioner = Provisioner(settings, store)
        self.telegram = TelegramBotClient(settings.telegram_bot_token)

    @property
    def support_url(self) -> str:
        return self.settings.support_tg_url or "https://t.me/SilentConnectHelp"

    @property
    def bot_url(self) -> str:
        username = self.settings.telegram_bot_username or "SilentConnectVPNBot"
        return f"https://t.me/{username}?start=open"

    def payment_transfer_url(self, order: dict[str, Any] | None = None) -> str:
        return (self.settings.payment_transfer_url or "").strip()

    def payment_bank_note(self) -> str:
        return (
            (self.settings.payment_bank_note or "").strip()
            or "Приоритетно переводить в МТС Банк. Если удобнее, можно Ozon Банк или Т-Банк."
        )

    def order_url(self, headers: Any, order: dict[str, Any]) -> str:
        token = str((order.get("meta_json") or {}).get("web_token") or "")
        return f"{public_origin(headers, self.settings.web_public_base_url)}/order/{quote(str(order['public_id']))}/{quote(token)}"

    def claim_url(self, order: dict[str, Any]) -> str:
        username = self.settings.telegram_bot_username or "SilentConnectVPNBot"
        token = str((order.get("meta_json") or {}).get("web_token") or "")
        return f"https://t.me/{username}?start=claim_{order['public_id']}_{token}"

    def recover_profile_sub_id(self, profile_public_id: str) -> str | None:
        profile = self.store.get_profile(profile_public_id)
        if not profile:
            return None
        found = self.provisioner.xui_db.find_client_by_email(str(profile["xui_email"]))
        if not found:
            return None
        sub_id = str((found.get("client") or {}).get("subId") or "")
        return sub_id or None

    def recover_subscription(self, order: dict[str, Any]) -> str | None:
        meta = order.get("meta_json") or {}
        if meta.get("hybrid") and meta.get("tcp_profile_public_id") and meta.get("xhttp_profile_public_id"):
            tcp_sub_id = self.recover_profile_sub_id(str(meta["tcp_profile_public_id"]))
            xhttp_sub_id = self.recover_profile_sub_id(str(meta["xhttp_profile_public_id"]))
            if not tcp_sub_id or not xhttp_sub_id:
                return None
            return subscription_url_for_route(
                self.settings.subscription_base_url,
                "json-hybrid",
                f"{tcp_sub_id}~{xhttp_sub_id}",
            )
        profile = self.store.get_profile_for_order(str(order["public_id"]))
        if not profile:
            return None
        found = self.provisioner.xui_db.find_client_by_email(str(profile["xui_email"]))
        if not found:
            return None
        sub_id = str((found.get("client") or {}).get("subId") or "")
        if not sub_id:
            return None
        return f"{self.settings.subscription_base_url}/{quote(sub_id, safe='')}"

    def offer_by_code(self, code: str) -> Offer:
        offer = self.offers.get(code)
        if not offer:
            raise ValueError("unknown offer")
        return offer

    @staticmethod
    def promo_type(promo: dict[str, Any]) -> str:
        return str(promo.get("promo_type") or "fixed")

    def promo_device_limit(self, promo: dict[str, Any]) -> int:
        if promo.get("device_limit") is not None:
            return int(promo["device_limit"])
        return int(self.settings.default_device_limit)

    def base_price_for_device_limit(self, device_limit: int | str | None) -> int:
        limit = int(self.settings.default_device_limit if device_limit is None else device_limit)
        if limit <= 0:
            return self.settings.monthly_price_9_devices_rub
        if limit <= 3:
            return self.settings.monthly_price_3_devices_rub
        if limit <= 6:
            return self.settings.monthly_price_6_devices_rub
        return self.settings.monthly_price_9_devices_rub

    def base_price_for_duration(self, transport: str, duration_days: int, *, device_limit: int | str | None = None) -> int:
        monthly_price = self.base_price_for_device_limit(device_limit)
        if transport == "hybrid":
            monthly_price = (monthly_price * 120 + 99) // 100
        return max((monthly_price * int(duration_days) + 29) // 30, 0)

    @staticmethod
    def fixed_promo_final_price(promo: dict[str, Any], base_price: int) -> int:
        fixed_price = promo.get("fixed_price_rub")
        if fixed_price is not None:
            return max(int(fixed_price), 0)
        return max(int(base_price) * (100 - int(promo["discount_percent"])) // 100, 0)

    def load_valid_promo(self, code: str) -> dict[str, Any]:
        promo = self.store.find_valid_promo(code.strip())
        if not promo:
            raise ValueError("Промокод не найден или уже недействителен.")
        return promo

    def ensure_promo_not_reserved(self, promo: dict[str, Any]) -> None:
        reserved = self.store.get_open_order_for_promo(int(promo["id"]))
        if reserved:
            raise ValueError("Промокод уже применён в другом открытом заказе. Если это ошибка, напишите в поддержку.")

    def maybe_auto_deliver_free_web_order(self, order: dict[str, Any]) -> dict[str, Any]:
        if int(order.get("final_price_rub") or 0) > 0:
            return order
        result = self.provisioner.create_profile_for_order(order)
        self.store.update_order_status(str(order["public_id"]), "delivered", closed=True)
        if order.get("promo_id"):
            self.store.mark_promo_used(int(order["promo_id"]))
        self.store.record_admin_action(
            action_type="web_auto_free_order",
            target_type="order",
            target_public_id=str(order["public_id"]),
            actor="web",
            meta={"transport": order["transport"], "profile_public_id": result["profile"]["public_id"]},
        )
        return self.store.get_order(str(order["public_id"])) or order

    def create_order(self, offer_code: str, promo_code: str = "") -> dict[str, Any]:
        offer = self.offer_by_code(offer_code)
        token = secrets.token_hex(12)
        promo = self.load_valid_promo(promo_code) if promo_code.strip() else None
        if promo and self.promo_type(promo) != "discount":
            raise ValueError("Этот промокод даёт готовый доступ. Введите его в блоке промокода, без выбора тарифа.")
        if promo:
            self.ensure_promo_not_reserved(promo)
        discount_percent = int(promo["discount_percent"]) if promo else 0
        final_price = max(offer.price_rub * (100 - discount_percent) // 100, 0)
        meta = {
            "source": offer.code,
            "device_limit": offer.device_limit,
            "web": True,
            "web_token": token,
        }
        if promo:
            meta["promo_type"] = "discount"
        order = self.store.create_order(
            kind="purchase",
            status="waiting_payment" if final_price > 0 else "auto_provision",
            transport=offer.transport,
            duration_days=offer.duration_days,
            profile_mode=offer.profile_mode,
            family_label=None,
            base_price_rub=offer.price_rub,
            final_price_rub=final_price,
            promo_id=int(promo["id"]) if promo else None,
            invite_id=None,
            customer_chat_id=None,
            privacy_ack=True,
            loss_policy_ack=True,
            terms_version=self.settings.terms_version,
            meta=meta,
        )
        return self.maybe_auto_deliver_free_web_order(order)

    def create_promo_order(self, promo_code: str) -> dict[str, Any]:
        promo = self.load_valid_promo(promo_code)
        if self.promo_type(promo) == "discount":
            raise ValueError("Промокод принят как скидка. Выберите тариф ниже, и цена пересчитается.")
        self.ensure_promo_not_reserved(promo)
        token = secrets.token_hex(12)
        duration_days = int(promo["duration_days"])
        device_limit = self.promo_device_limit(promo)
        base_price = self.base_price_for_duration(str(promo["transport"]), duration_days, device_limit=device_limit)
        final_price = self.fixed_promo_final_price(promo, base_price)
        meta = {
            "source": "web_promo",
            "promo_type": "fixed",
            "device_limit": device_limit,
            "web": True,
            "web_token": token,
        }
        if promo.get("duration_months"):
            meta["duration_months"] = int(promo["duration_months"])
        if promo.get("fixed_price_rub") is not None:
            meta["fixed_price_rub"] = int(promo["fixed_price_rub"])
        order = self.store.create_order(
            kind="purchase",
            status="waiting_payment" if final_price > 0 else "auto_provision",
            transport=str(promo["transport"]),
            duration_days=duration_days,
            profile_mode=str(promo["profile_mode"]),
            family_label=promo.get("family_label"),
            base_price_rub=base_price,
            final_price_rub=final_price,
            promo_id=int(promo["id"]),
            invite_id=None,
            customer_chat_id=None,
            privacy_ack=True,
            loss_policy_ack=True,
            terms_version=self.settings.terms_version,
            meta=meta,
        )
        return self.maybe_auto_deliver_free_web_order(order)

    def load_web_order(self, order_public_id: str, token: str) -> dict[str, Any] | None:
        order = self.store.get_order(order_public_id)
        if not order:
            return None
        meta = order.get("meta_json") or {}
        if not meta.get("web") or str(meta.get("web_token") or "") != token:
            return None
        return order

    def mark_paid(self, headers: Any, order: dict[str, Any]) -> str:
        meta = dict(order.get("meta_json") or {})
        if order.get("status") != "waiting_payment":
            return "Этот заказ уже не ожидает оплату."

        last = self.store.get_last_admin_action(
            action_type="web_payment_reported_by_customer",
            target_type="order",
            target_public_id=str(order["public_id"]),
        )
        repeated = last and now_ts() - int(last.get("created_at") or 0) < PAYMENT_REPORT_REPEAT_SECONDS
        if repeated:
            return "Уведомление уже отправлено. Проверьте эту страницу чуть позже."

        meta["web_paid_reported_at"] = now_ts()
        self.store.update_order_meta(str(order["public_id"]), meta)
        self.store.record_admin_action(
            action_type="web_payment_reported_by_customer",
            target_type="order",
            target_public_id=str(order["public_id"]),
            actor="web",
            meta={"order_url": self.order_url(headers, {**order, "meta_json": meta})},
        )

        text = "\n".join(
            [
                "Покупатель с сайта сообщает об оплате",
                "",
                f"Заказ: `{order['public_id']}`",
                f"Транспорт: {transport_label(str(order['transport']))}",
                f"Срок: {int(order['duration_days'])} дн.",
                f"Лимит: {device_limit_label(meta.get('device_limit'))}",
                f"Сумма: {money(order['final_price_rub'])}",
                "",
                "Проверьте поступление и подтвердите оплату.",
            ]
        )
        admin_chats = self.store.list_chat_ids_by_scope("admin")
        for chat_id in admin_chats:
            try:
                sent = self.telegram.send_message(chat_id, text, reply_markup=inline_admin_markup(str(order["public_id"])))
                self.store.attach_manager_message(str(order["public_id"]), chat_id, int(sent["message_id"]))
            except TelegramApiError:
                LOGGER.exception("Failed to notify admin chat %s about web order %s", chat_id, order["public_id"])
        return "Уведомление отправлено. После проверки оплаты на этой странице появится доступ."

    def order_status_json(self, order: dict[str, Any]) -> bytes:
        meta = order.get("meta_json") or {}
        payload = {
            "ok": True,
            "status": str(order.get("status") or ""),
            "paid_reported": bool(meta.get("web_paid_reported_at")),
            "updated_at": int(order.get("updated_at") or 0),
        }
        if payload["status"] == "delivered":
            subscription_url = self.recover_subscription(order)
            if subscription_url:
                payload["setup_url"] = subscription_setup_url(subscription_url)
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def order_poll_script(self, order: dict[str, Any], *, interval_seconds: int = 5) -> str:
        meta = order.get("meta_json") or {}
        status_url = f"/order/{quote(str(order['public_id']), safe='')}/{quote(str(meta.get('web_token') or ''), safe='')}/status"
        return f"""
        <script>
        (() => {{
          const initialStatus = {json.dumps(str(order.get("status") or ""))};
          const statusUrl = {json.dumps(status_url)};
          const statusBox = document.getElementById("order-live-status");
          let failures = 0;

          async function pollOrderStatus() {{
            try {{
              const response = await fetch(statusUrl, {{ cache: "no-store", credentials: "same-origin" }});
              if (!response.ok) return;
              const data = await response.json();
              failures = 0;

              if (data.status && data.status !== initialStatus) {{
                if (statusBox) {{
                  statusBox.textContent = data.status === "delivered"
                    ? "Оплата подтверждена. Открываем доступ..."
                    : "Статус заказа изменился. Обновляем страницу...";
                }}
                if (data.status === "delivered" && data.setup_url) {{
                  window.location.href = data.setup_url;
                  return;
                }}
                window.location.reload();
                return;
              }}

              if (statusBox && data.paid_reported) {{
                statusBox.textContent = "Оплата отмечена. Ждём подтверждение админом, эта страница обновится сама.";
              }}
            }} catch (error) {{
              failures += 1;
              if (statusBox && failures >= 3) {{
                statusBox.textContent = "Проверяем статус. Если страница долго не обновляется, обновите её вручную.";
              }}
            }}
          }}

          window.setTimeout(pollOrderStatus, 1500);
          window.setInterval(pollOrderStatus, {int(interval_seconds) * 1000});
        }})();
        </script>
        """

    def render_page(self, title: str, body: str, *, refresh_seconds: int | None = None) -> bytes:
        refresh = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">' if refresh_seconds else ""
        notice_html = f'<div class="notice site-alert" role="status">{html.escape(TEMP_NETWORK_NOTICE)}</div>' if TEMP_NETWORK_NOTICE else ""
        html_doc = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh}
  <title>{html.escape(title)} · SilentConnect</title>
  <link rel="icon" type="image/png" href="/assets/telegram/avatar.png">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {{
      color-scheme: dark;
      --bg-dark: #050a08;
      --bg-light: #0a1410;
      --glass-bg: rgba(20, 35, 28, 0.4);
      --glass-bg-hover: rgba(30, 50, 40, 0.6);
      --glass-border: rgba(255, 255, 255, 0.08);
      --text: #f4f7f5;
      --muted: #8ea89a;
      --line: rgba(255, 255, 255, 0.05);
      --green: #2fbf71;
      --green-glow: rgba(47, 191, 113, 0.3);
      --blue: #3b82f6;
      --red: #ef4444;
      --amber: #f59e0b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
      background-color: var(--bg-dark);
      background-image: 
        radial-gradient(circle at 15% 50%, rgba(47, 191, 113, 0.08), transparent 25%),
        radial-gradient(circle at 85% 30%, rgba(59, 130, 246, 0.08), transparent 25%);
      background-attachment: fixed;
      color: var(--text);
      line-height: 1.6;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }}
    h1, h2, h3, .brand, .price, strong, .btn, button {{
      font-family: 'Outfit', sans-serif;
    }}
    a {{ color: inherit; transition: color 0.2s; }}
    .wrap {{ width: min(1120px, calc(100% - 40px)); margin: 0 auto; flex: 1; }}
    header {{ 
      background: rgba(5, 10, 8, 0.7); 
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border-bottom: 1px solid var(--glass-border); 
      position: sticky; top: 0; z-index: 10; 
    }}
    nav {{ height: 72px; display: flex; align-items: center; justify-content: space-between; gap: 16px; }}
    .brand {{ 
      font-weight: 800; font-size: 22px; letter-spacing: -0.5px;
      background: linear-gradient(to right, #fff, #2fbf71);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }}
    .navlinks {{ display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }}
    .navlinks a {{ text-decoration: none; color: var(--muted); font-weight: 500; font-size: 15px; }}
    .navlinks a:hover {{ color: #fff; text-shadow: 0 0 10px rgba(255,255,255,0.3); }}
    .hero {{ display: grid; grid-template-columns: 1fr 1fr; gap: 40px; padding: 60px 0 40px; align-items: center; }}
    .hero h1 {{ font-size: clamp(36px, 5vw, 64px); line-height: 1.1; margin: 0 0 20px; letter-spacing: -1px; }}
    .lead {{ color: var(--muted); font-size: 19px; max-width: 680px; margin: 0 0 24px; font-weight: 400; }}
    .hero-img {{ 
      width: 100%; border-radius: 16px; 
      border: 1px solid var(--glass-border); 
      box-shadow: 0 20px 40px rgba(0,0,0,0.4), 0 0 40px var(--green-glow);
      opacity: .96;
      transition: transform 0.5s cubic-bezier(0.175, 0.885, 0.32, 1.275);
    }}
    .hero-img:hover {{ transform: translateY(-5px) scale(1.02); }}
    .section {{ padding: 40px 0; }}
    .section-head {{ display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 24px; }}
    .section-head p {{ margin: 0; max-width: 620px; color: var(--muted); font-size: 16px; }}
    h2 {{ font-size: 32px; margin: 0; letter-spacing: -0.5px; font-weight: 700; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
    .advantage-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 0 0 18px; }}
    .advantage {{
      min-height: 132px; padding: 18px; border-radius: 8px;
      background: rgba(255,255,255,0.055); border: 1px solid var(--glass-border);
    }}
    .advantage b {{ display: block; color: #fff; margin-bottom: 6px; font-family: 'Outfit', sans-serif; font-size: 17px; }}
    .advantage span {{ display: block; color: var(--muted); font-size: 14px; line-height: 1.45; }}
    .addon-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
    .card {{ 
      background: var(--glass-bg);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border: 1px solid var(--glass-border); 
      border-radius: 16px; 
      padding: 24px;
      transition: all 0.3s ease;
      box-shadow: 0 4px 20px rgba(0,0,0,0.2);
    }}
    .plan-card {{ display: flex; flex-direction: column; min-height: 250px; position: relative; overflow: hidden; }}
    .plan-card::before {{
      content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px;
      background: linear-gradient(90deg, var(--green), transparent); opacity: 0; transition: opacity 0.3s;
    }}
    .plan-card:hover {{
      transform: translateY(-4px);
      background: var(--glass-bg-hover);
      box-shadow: 0 12px 30px rgba(0,0,0,0.3), 0 0 20px rgba(47, 191, 113, 0.1);
      border-color: rgba(255, 255, 255, 0.15);
    }}
    .plan-card:hover::before {{ opacity: 1; }}
    .plan-card button {{ margin-top: auto; }}
    .card.addon {{ background: rgba(30, 40, 45, 0.3); }}
    .card.addon:hover {{ background: rgba(40, 55, 60, 0.5); border-color: rgba(59, 130, 246, 0.2); }}
    .card.addon::before {{ background: linear-gradient(90deg, var(--blue), transparent); }}
    .card strong {{ display: block; font-size: 20px; margin-bottom: 8px; letter-spacing: -0.2px; }}
    .plan-title {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }}
    .plan-title strong {{ margin-bottom: 0; font-size: 22px; }}
    .muted {{ color: var(--muted); }}
    .fine {{ color: var(--muted); font-size: 13px; margin: 12px 0 0; opacity: 0.8; }}
    .price {{ font-size: 36px; font-weight: 800; margin: 16px 0; color: #fff; }}
    .badge {{ 
      color: #000; background: var(--amber); 
      border-radius: 6px; padding: 4px 10px; 
      font-weight: 700; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px;
      box-shadow: 0 0 10px rgba(245, 158, 11, 0.3);
    }}
    .btn, button {{
      display: inline-flex; justify-content: center; align-items: center; min-height: 48px;
      padding: 12px 20px; border-radius: 10px; border: none;
      color: #000; background: var(--green); font-weight: 700; text-decoration: none; cursor: pointer;
      font-size: 16px; width: 100%; letter-spacing: 0.2px;
      transition: all 0.2s ease;
      box-shadow: 0 4px 12px var(--green-glow);
    }}
    .btn:hover, button:hover {{
      transform: translateY(-2px);
      box-shadow: 0 6px 16px rgba(47, 191, 113, 0.4);
      filter: brightness(1.1);
    }}
    .btn:active, button:active {{ transform: translateY(0); box-shadow: none; }}
    .btn.secondary {{ background: rgba(255,255,255,0.1); color: #fff; box-shadow: none; border: 1px solid var(--glass-border); backdrop-filter: blur(4px); }}
    .btn.secondary:hover {{ background: rgba(255,255,255,0.15); border-color: rgba(255,255,255,0.2); }}
    .btn.blue {{ background: var(--blue); color: #fff; box-shadow: 0 4px 12px rgba(59, 130, 246, 0.3); }}
    .btn.red {{ background: var(--red); color: #fff; box-shadow: 0 4px 12px rgba(239, 68, 68, 0.3); }}
    .actions {{ display: grid; gap: 12px; margin-top: 20px; }}
    .builder {{ display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(280px, .8fr); gap: 20px; align-items: stretch; }}
    .choice-section {{ margin-top: 20px; }}
    .choice-section:first-child {{ margin-top: 0; }}
    .choice-title {{ font-weight: 700; margin-bottom: 10px; color: #fff; }}
    .choice-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }}
    .choice-grid.two {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .choice {{
      position: relative;
      width: 100%; min-height: 76px; padding: 12px; text-align: left; justify-content: flex-start;
      align-items: flex-start; flex-direction: column; gap: 3px;
      color: #e9f3ee; background: rgba(255,255,255,0.06); box-shadow: none;
      border: 1px solid var(--glass-border); border-radius: 12px;
    }}
    .choice:hover {{ transform: translateY(-1px); filter: none; background: rgba(255,255,255,0.09); box-shadow: none; }}
    .choice.active {{ border-color: var(--green); background: rgba(47, 191, 113, 0.16); box-shadow: 0 0 0 1px rgba(47, 191, 113, 0.16) inset; }}
    .choice span {{ color: var(--muted); font-family: 'Inter', sans-serif; font-size: 13px; font-weight: 500; }}
    .summary-box {{ position: sticky; top: 96px; display: flex; flex-direction: column; }}
    .summary-row {{ display: flex; justify-content: space-between; gap: 14px; border-bottom: 1px solid var(--line); padding: 10px 0; }}
    .summary-row span:first-child {{ color: var(--muted); }}
    .summary-price {{ font-size: 42px; line-height: 1; font-weight: 800; margin: 18px 0; }}
    .summary-note {{ color: var(--muted); font-size: 14px; margin: 0 0 16px; }}
    .notice {{ 
      border-left: 4px solid var(--amber); 
      background: rgba(245, 158, 11, 0.1); 
      padding: 16px; border-radius: 0 8px 8px 0; 
      color: #fff; font-size: 15px; 
      backdrop-filter: blur(8px);
    }}
    .site-alert {{ margin: 20px 0 0; }}
    .order {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; align-items: start; }}
    .mono {{ font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Consolas, monospace; word-break: break-all; background: rgba(0,0,0,0.3); padding: 12px; border-radius: 8px; border: 1px solid var(--glass-border); }}
    .field {{ width: 100%; min-height: 48px; color: #fff; background: rgba(0,0,0,0.3); border: 1px solid var(--glass-border); border-radius: 10px; padding: 12px 14px; font-size: 16px; }}
    .field:focus {{ outline: none; border-color: var(--green); }}
    .price-old {{ color: var(--muted); text-decoration: line-through; font-size: 18px; margin-left: 8px; }}
    textarea {{ 
      width: 100%; min-height: 100px; resize: vertical; 
      color: #fff; background: rgba(0,0,0,0.3); 
      border: 1px solid var(--glass-border); border-radius: 10px; padding: 14px; 
      font-family: inherit; font-size: 14px;
      transition: border-color 0.2s;
    }}
    textarea:focus {{ outline: none; border-color: var(--green); }}
    footer {{ 
      margin-top: auto;
      border-top: 1px solid var(--glass-border); 
      padding: 32px 0; color: var(--muted); font-size: 14px; 
      background: rgba(5, 10, 8, 0.5);
    }}
    @media (max-width: 820px) {{
      .hero, .order {{ grid-template-columns: 1fr; gap: 24px; padding: 30px 0; }}
      .hero h1 {{ font-size: 32px; }}
      .advantage-grid {{ grid-template-columns: 1fr; }}
      .grid, .addon-grid {{ grid-template-columns: 1fr; }}
      .builder {{ grid-template-columns: 1fr; }}
      .choice-grid, .choice-grid.two {{ grid-template-columns: 1fr; }}
      .summary-box {{ position: static; }}
      .section-head {{ flex-direction: column; align-items: flex-start; }}
      .section-head p {{ margin-top: 8px; }}
      nav {{ height: auto; padding: 16px 0; align-items: flex-start; flex-direction: column; gap: 12px; }}
    }}
  </style>
</head>
<body>
  <header><nav class="wrap"><div class="brand">SilentConnect</div><div class="navlinks"><a href="/">Тарифы</a><a href="{html.escape(self.bot_url)}">Telegram-бот</a><a href="{html.escape(self.support_url)}">Поддержка</a></div></nav></header>
  <main class="wrap">{notice_html}{body}</main>
  <footer><div class="wrap">SilentConnect · Оплата подтверждается вручную · Пробная неделя и продления доступны в Telegram после подключения.</div></footer>
  <script>
    async function copyText(id) {{
      const el = document.getElementById(id);
      if (!el) return;
      await navigator.clipboard.writeText(el.value || el.textContent);
    }}
  </script>
</body>
</html>"""
        return html_doc.encode("utf-8")

    def render_home(
        self,
        *,
        active_discount: dict[str, Any] | None = None,
        promo_code: str = "",
        flash: str = "",
    ) -> bytes:
        discount_percent = int(active_discount["discount_percent"]) if active_discount else 0
        escaped_promo_code = html.escape(promo_code.strip())
        flash_html = f'<div class="notice">{html.escape(flash)}</div>' if flash else ""
        promo_hidden = f'<input type="hidden" name="promo_code" value="{escaped_promo_code}">' if active_discount else ""
        discount_line = f" Активна скидка {discount_percent}%." if active_discount else ""

        plans: dict[str, dict[str, Any]] = {}
        for device_limit in (3, 6, 9):
            for duration_days in (30, 90, 180, 360):
                for mode in ("tcp", "hybrid"):
                    offer = self.offers[f"{mode}_{device_limit}_{duration_days}"]
                    final_price = max(offer.price_rub * (100 - discount_percent) // 100, 0)
                    plans[offer.code] = {
                        "code": offer.code,
                        "title": "Универсальный" if mode == "hybrid" else "Стандартный",
                        "deviceTitle": {3: "Личный", 6: "Домашний", 9: "Расширенный"}[device_limit],
                        "deviceLimit": device_limit,
                        "duration": {30: "1 месяц", 90: "3 месяца", 180: "6 месяцев", 360: "12 месяцев"}[duration_days],
                        "price": final_price,
                        "basePrice": offer.price_rub,
                        "discount": discount_percent,
                    }
        plans_json = json.dumps(plans, ensure_ascii=False)
        initial_price = int(plans["tcp_3_30"]["price"])

        body = f"""
        <section class="hero">
          <div>
            <h1>Незаметный VPN, который просто работает</h1>
            <p class="lead">Включили один раз — и забыли. Выберите тариф, получите готовую ссылку для приложения и пользуйтесь открытым интернетом. Поможем, если что-то не получится.</p>
            <div class="notice">Пробная неделя и продление доступны в Telegram-боте после подключения</div>
          </div>
          <img class="hero-img" src="/assets/telegram/welcome.png" alt="SilentConnect">
        </section>
        <section class="section" style="padding-top: 0;">
          <div class="advantage-grid">
            <div class="advantage">
              <b>Готовая подписка</b>
              <span>Одна ссылка для телефона и ПК, с автообновлением профилей и понятной страницей подключения.</span>
            </div>
            <div class="advantage">
              <b>Устойчивые режимы</b>
              <span>Стандартный и универсальный доступ помогают переживать разные типы сетевых ограничений.</span>
            </div>
            <div class="advantage">
              <b>Резерв под белые списки</b>
              <span>Тестируем OLCbox-контур для случаев, когда обычные маршруты режутся домашней сетью или провайдером.</span>
            </div>
            <div class="advantage">
              <b>Живая поддержка</b>
              <span>Помогаем с Happ, iOS, Android и Windows, а продления и напоминания ведет Telegram-бот.</span>
            </div>
          </div>
        </section>
        <section class="section order">
          <div class="card">
            <strong>Есть промокод?</strong>
            <p class="muted">Введите код здесь. Скидочный промокод пересчитает тарифы, а промокод на готовый доступ сразу откроет заказ.</p>
            {flash_html}
            <form method="post" action="/promo" class="actions">
              <input class="field" name="promo_code" value="{escaped_promo_code}" placeholder="PROMO-XXXX-XXXX" autocomplete="off">
              <button type="submit">Применить промокод</button>
            </form>
          </div>
          <div class="card">
            <strong>После оплаты</strong>
            <p class="muted">На этой же странице появится ссылка для приложения. Для продления и пробной недели потом привяжите подписку в Telegram-боте.</p>
            <a class="btn secondary" href="{html.escape(self.bot_url)}">Открыть Telegram-бота</a>
          </div>
        </section>
        <section class="section">
          <div class="section-head">
            <h2>Соберите тариф</h2>
            <p class="muted">Выберите устройства, срок и режим без перезагрузки страницы. Технические настройки уже подготовлены.{html.escape(discount_line)}</p>
          </div>
          <div class="builder">
            <div class="card">
              <div class="choice-section">
                <div class="choice-title">Количество устройств</div>
                <div class="choice-grid">
                  <button type="button" class="choice active" data-choice data-group="device" data-value="3">Личный<span>до 3 устройств</span></button>
                  <button type="button" class="choice" data-choice data-group="device" data-value="6">Домашний<span>до 6 устройств</span></button>
                  <button type="button" class="choice" data-choice data-group="device" data-value="9">Расширенный<span>до 9 устройств</span></button>
                </div>
              </div>
              <div class="choice-section">
                <div class="choice-title">Срок доступа</div>
                <div class="choice-grid">
                  <button type="button" class="choice active" data-choice data-group="duration" data-value="30">1 месяц<span>помесячно</span></button>
                  <button type="button" class="choice" data-choice data-group="duration" data-value="90"><span class="badge" style="position: absolute; top: -10px; right: -5px; font-size: 10px; padding: 2px 6px; background: var(--green); color: #fff; box-shadow: 0 0 8px rgba(47, 191, 113, 0.4);">−10%</span>3 месяца<span>экономия</span></button>
                  <button type="button" class="choice" data-choice data-group="duration" data-value="180"><span class="badge" style="position: absolute; top: -10px; right: -5px; font-size: 10px; padding: 2px 6px; background: var(--green); color: #fff; box-shadow: 0 0 8px rgba(47, 191, 113, 0.4);">−20%</span>6 месяцев<span>выгодно</span></button>
                  <button type="button" class="choice" data-choice data-group="duration" data-value="360"><span class="badge" style="position: absolute; top: -10px; right: -5px; font-size: 10px; padding: 2px 6px; background: var(--amber); color: #000; box-shadow: 0 0 8px rgba(245, 158, 11, 0.4);">−30%</span>12 месяцев<span>максимум</span></button>
                </div>
              </div>
            </div>
            <form class="card summary-box" method="post" action="/order">
              <strong id="builder-title">Личный</strong>
              <p class="summary-note" id="builder-subtitle">До 3 устройств · 1 месяц</p>
              <div class="summary-row"><span>Устройства</span><b id="builder-devices">до 3</b></div>
              <div class="summary-row"><span>Срок</span><b id="builder-duration">1 месяц</b></div>
              <div class="summary-price" id="builder-price">{initial_price} ₽</div>
              <p class="summary-note">После оплаты на странице заказа появится готовая ссылка для подключения.</p>
              <input id="builder-offer" type="hidden" name="offer" value="tcp_3_30">
              {promo_hidden}
              <button type="submit">Перейти к оплате</button>
            </form>
          </div>
        </section>
        <section class="section">
          <div class="section-head">
            <h2>3 простых шага к свободному интернету</h2>
            <p class="muted">Вам не нужно быть программистом — всё настраивается за пару минут.</p>
          </div>
          <div class="grid">
            <div class="card">
              <strong style="color: var(--green); font-size: 32px; margin-bottom: 12px;">01</strong>
              <strong>Выберите тариф и оплатите</strong>
              <p class="muted" style="font-size: 15px;">Выберите количество устройств и срок доступа в калькуляторе выше, нажмите «Перейти к оплате», переведите сумму на карту или СБП и подтвердите платёж.</p>
            </div>
            <div class="card">
              <strong style="color: var(--green); font-size: 32px; margin-bottom: 12px;">02</strong>
              <strong>Скачайте приложение</strong>
              <p class="muted" style="font-size: 15px;">Нажмите кнопку «Подключить» на странице заказа. Она сама определит ваше устройство (iPhone, Android, ПК) и предложит скачать приложение в один клик.</p>
            </div>
            <div class="card">
              <strong style="color: var(--green); font-size: 32px; margin-bottom: 12px;">03</strong>
              <strong>Импортируйте ссылку</strong>
              <p class="muted" style="font-size: 15px;">Нажмите на кнопку «Импортировать в приложение» или скопируйте ссылку подписки и вставьте её. Всё готово — включайте и пользуйтесь!</p>
            </div>
          </div>
        </section>
        <section class="section">
          <div class="section-head">
            <h2>Ответы на вопросы (FAQ)</h2>
            <p class="muted">Простые ответы для всех пользователей.</p>
          </div>
          <div class="grid">
            <div class="card">
              <strong>Будут ли работать нужные мне сайты?</strong>
              <p class="muted" style="font-size: 14px;">Да! Наш VPN использует маскировку трафика Reality и Hysteria 2. Instagram, YouTube, ChatGPT, Kinopoisk и другие ресурсы будут открываться мгновенно и без задержек.</p>
            </div>
            <div class="card">
              <strong>На каких устройствах работает VPN?</strong>
              <p class="muted" style="font-size: 14px;">На любых! Вы можете установить его на iPhone, iPad, Android-смартфоны, планшеты, а также на компьютеры Windows, macOS и Linux.</p>
            </div>
            <div class="card">
              <strong>Почему оплата подтверждается вручную?</strong>
              <p class="muted" style="font-size: 14px;">Чтобы не брать лишних комиссий за платёжные шлюзы и сохранять самые низкие цены. Вы делаете перевод, мы проверяем его и сразу даём доступ. Обычно это занимает не больше 5–10 минут.</p>
            </div>
            <div class="card">
              <strong>Что делать, если возникнут трудности?</strong>
              <p class="muted" style="font-size: 14px;">Не волнуйтесь! Нажмите кнопку «Поддержка» или напишите нам в Telegram (кнопка в шапке сайта). Мы поможем вам настроить всё по шагам.</p>
            </div>
          </div>
        </section>
        <script>
        (() => {{
          const plans = {plans_json};
          const state = {{ device: "3", duration: "30", mode: "tcp" }};
          const rub = new Intl.NumberFormat("ru-RU").format;
          const el = (id) => document.getElementById(id);

          function setActive(group, value) {{
            document.querySelectorAll('[data-choice][data-group="' + group + '"]').forEach((button) => {{
              button.classList.toggle("active", button.dataset.value === value);
            }});
          }}

          function update() {{
            const code = state.mode + "_" + state.device + "_" + state.duration;
            const plan = plans[code];
            if (!plan) return;
            el("builder-title").textContent = plan.deviceTitle;
            el("builder-subtitle").textContent = "До " + plan.deviceLimit + " устройств · " + plan.duration;
            el("builder-devices").textContent = "до " + plan.deviceLimit;
            el("builder-duration").textContent = plan.duration;
            el("builder-offer").value = plan.code;
            el("builder-price").textContent = rub(plan.price) + " ₽";
            if (plan.discount) {{
              el("builder-price").textContent = rub(plan.price) + " ₽ вместо " + rub(plan.basePrice) + " ₽";
            }}
          }}

          document.querySelectorAll("[data-choice]").forEach((button) => {{
            button.addEventListener("click", () => {{
              state[button.dataset.group] = button.dataset.value;
              setActive(button.dataset.group, button.dataset.value);
              update();
            }});
          }});
          update();
        }})();
        </script>
        """
        return self.render_page("VPN-доступ", body)

    def render_order(self, headers: Any, order: dict[str, Any], *, flash: str = "") -> bytes:
        meta = order.get("meta_json") or {}
        order_url = self.order_url(headers, order)
        flash_html = f'<div class="notice">{html.escape(flash)}</div>' if flash else ""
        summary = f"""
        <div class="card">
          <strong>Заказ {html.escape(str(order['public_id']))}</strong>
          <p class="muted">Транспорт: {html.escape(transport_label(str(order['transport'])))}<br>
          Срок: {int(order['duration_days'])} дней<br>
          Лимит: {html.escape(device_limit_label(meta.get('device_limit')))}<br>
          Сумма: {html.escape(money(order['final_price_rub']))}</p>
          <p class="muted">Сохраните эту страницу. По ней можно вернуться к статусу заказа.</p>
          <textarea id="order-link" readonly>{html.escape(order_url)}</textarea>
          <div class="actions"><button type="button" onclick="copyText('order-link')">Скопировать ссылку заказа</button></div>
        </div>
        """
        status = str(order.get("status") or "")
        if status == "waiting_payment":
            paid_reported = bool(meta.get("web_paid_reported_at"))
            payment_url = html.escape(self.payment_transfer_url(order), quote=True)
            bank_note = html.escape(self.payment_bank_note())
            pay_button = (
                f'<a class="btn" href="{payment_url}" target="_blank" rel="noopener">Оплатить переводом</a>'
                if payment_url
                else f'<a class="btn secondary" href="{html.escape(self.support_url)}">Получить ссылку на оплату</a>'
            )
            payment_action = (
                """
                <div id="order-live-status" class="notice">Оплата отмечена. Ждём подтверждение админом, эта страница обновится сама.</div>
                """
                if paid_reported
                else f"""
                <p class="muted">Нажмите кнопку оплаты, переведите ровно {html.escape(money(order['final_price_rub']))}, затем отметьте заказ как оплаченный.</p>
                <div class="actions">{pay_button}</div>
                <p class="fine">Комментарий к переводу можно оставить нейтральным: {html.escape(str(order['public_id']))}. {bank_note}</p>
                <form method="post" action="/order/{html.escape(str(order['public_id']))}/{html.escape(str(meta.get('web_token') or ''))}/paid">
                  <button type="submit">Оплачено</button>
                </form>
                """
            )
            body = f"""
            <section class="section"><h2>Оплата заказа</h2>{flash_html}</section>
            <section class="order">
              {summary}
              <div class="card">
                <strong>Оплата переводом</strong>
                {payment_action}
                <div class="actions"><a class="btn secondary" href="{html.escape(self.support_url)}">Поддержка</a></div>
              </div>
            </section>
            {self.order_poll_script(order)}
            """
            return self.render_page("Оплата заказа", body, refresh_seconds=12 if paid_reported else None)

        if status == "delivered":
            subscription_url = self.recover_subscription(order)
            if not subscription_url:
                body = f"<section class=\"section\"><h2>Доступ подтверждён</h2>{flash_html}<div class=\"notice\">Профиль создан, но ссылка не восстановилась. Напишите в поддержку.</div></section>"
                return self.render_page("Доступ подтверждён", body)
            setup_url = subscription_setup_url(subscription_url)
            claim_url = self.claim_url(order)
            body = f"""
            <section class="section"><h2>Доступ готов</h2>{flash_html}</section>
            <section class="order">
              {summary}
              <div class="card">
                <strong>Подключить VPN</strong>
                <p class="muted">Откройте страницу подключения. Она определит устройство, предложит подходящие приложения и покажет запасной способ через копирование.</p>
                <div class="actions">
                  <a class="btn" href="{html.escape(setup_url)}">Подключить</a>
                </div>
                <textarea id="sub-link" readonly>{html.escape(subscription_url)}</textarea>
                <p class="muted">Привяжите покупку в Telegram: бот узнает эту подписку, включит продление и напомнит за сутки и за час до окончания.</p>
                <div class="actions">
                  <button type="button" onclick="copyText('sub-link')">Скопировать ссылку</button>
                  <a class="btn secondary" href="{html.escape(claim_url)}">Привязать в Telegram для продления</a>
                </div>
              </div>
            </section>
            """
            return self.render_page("Доступ готов", body)

        if status == "cancelled":
            body = f"""
            <section class="section"><h2>Заказ отменён</h2>{flash_html}</section>
            <section class="order">{summary}<div class="card"><p class="muted">Можно оформить новый заказ или написать в поддержку.</p><div class="actions"><a class="btn" href="/">Выбрать тариф</a><a class="btn secondary" href="{html.escape(self.support_url)}">Поддержка</a></div></div></section>
            """
            return self.render_page("Заказ отменён", body)

        body = f"<section class=\"section\"><h2>Заказ обрабатывается</h2>{flash_html}</section><section class=\"order\">{summary}<div class=\"notice\">Статус: {html.escape(status)}</div></section>"
        body += self.order_poll_script(order)
        return self.render_page("Заказ обрабатывается", body, refresh_seconds=12)

    def render_not_found(self) -> bytes:
        body = '<section class="section"><h2>Страница не найдена</h2><p class="muted">Проверьте ссылку или откройте главную страницу.</p><a class="btn" href="/">На главную</a></section>'
        return self.render_page("Не найдено", body)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "SilentConnectWeb/1.0"
    checkout: WebCheckout

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, status: HTTPStatus, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, body: bytes) -> None:
        self._send(status, body, "application/json; charset=utf-8")

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 16_384)).decode("utf-8", errors="replace")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in parsed.items()}

    def _serve_asset(self, path: list[str]) -> bool:
        if path == ["assets", "telegram", "welcome.png"]:
            asset_name = "welcome.png"
        elif path == ["assets", "telegram", "avatar.png"]:
            asset_name = "avatar.png"
        else:
            return False
            
        asset = self.checkout.settings.root_dir / "assets" / "telegram" / asset_name
        if not asset.is_file():
            return False
        body = asset.read_bytes()
        self._send(HTTPStatus.OK, body, "image/png")
        return True

    def do_GET(self) -> None:
        try:
            parsed = urlsplit(self.path)
            path = [segment for segment in parsed.path.split("/") if segment]
            if not path:
                self._send(HTTPStatus.OK, self.checkout.render_home())
                return
            if self._serve_asset(path):
                return
            if len(path) == 4 and path[0] == "order" and path[3] == "status":
                order = self.checkout.load_web_order(path[1], path[2])
                if not order:
                    self._send_json(HTTPStatus.NOT_FOUND, b'{"ok":false}')
                    return
                self._send_json(HTTPStatus.OK, self.checkout.order_status_json(order))
                return
            if len(path) == 3 and path[0] == "order":
                order = self.checkout.load_web_order(path[1], path[2])
                if not order:
                    self._send(HTTPStatus.NOT_FOUND, self.checkout.render_not_found())
                    return
                self._send(HTTPStatus.OK, self.checkout.render_order(self.headers, order))
                return
            self._send(HTTPStatus.NOT_FOUND, self.checkout.render_not_found())
        except Exception:
            LOGGER.exception("GET failed")
            body = self.checkout.render_page("Ошибка", '<section class="section"><h2>Ошибка</h2><p class="muted">Попробуйте обновить страницу или напишите в поддержку.</p></section>')
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, body)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        try:
            parsed = urlsplit(self.path)
            path = [segment for segment in parsed.path.split("/") if segment]
            if path == ["order"]:
                form = self._read_form()
                order = self.checkout.create_order(form.get("offer", ""), form.get("promo_code", ""))
                self._redirect(self.checkout.order_url(self.headers, order))
                return
            if path == ["promo"]:
                form = self._read_form()
                promo_code = form.get("promo_code", "").strip()
                promo = self.checkout.load_valid_promo(promo_code)
                if self.checkout.promo_type(promo) == "discount":
                    self._send(
                        HTTPStatus.OK,
                        self.checkout.render_home(
                            active_discount=promo,
                            promo_code=promo_code,
                            flash=f"Промокод принят. Скидка {int(promo['discount_percent'])}% применится к выбранному тарифу.",
                        ),
                    )
                    return
                order = self.checkout.create_promo_order(promo_code)
                self._redirect(self.checkout.order_url(self.headers, order))
                return
            if len(path) == 4 and path[0] == "order" and path[3] == "paid":
                order = self.checkout.load_web_order(path[1], path[2])
                if not order:
                    self._send(HTTPStatus.NOT_FOUND, self.checkout.render_not_found())
                    return
                flash = self.checkout.mark_paid(self.headers, order)
                order = self.checkout.load_web_order(path[1], path[2]) or order
                self._send(HTTPStatus.OK, self.checkout.render_order(self.headers, order, flash=flash))
                return
            self._send(HTTPStatus.NOT_FOUND, self.checkout.render_not_found())
        except ValueError as exc:
            message = str(exc) or "Не удалось обработать запрос."
            self._send(
                HTTPStatus.BAD_REQUEST,
                self.checkout.render_home(flash=message),
            )
        except Exception:
            LOGGER.exception("POST failed")
            body = self.checkout.render_page("Ошибка", '<section class="section"><h2>Ошибка</h2><p class="muted">Попробуйте ещё раз или напишите в поддержку.</p></section>')
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, body)


def serve(settings: Settings, store: Store) -> None:
    checkout = WebCheckout(settings, store)
    RequestHandler.checkout = checkout
    server = ThreadingHTTPServer((settings.web_listen_host, settings.web_listen_port), RequestHandler)
    LOGGER.info("Serving web checkout on %s:%s", settings.web_listen_host, settings.web_listen_port)
    server.serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(Path(__file__).resolve().parents[1])
    store = Store(settings.database_path)
    serve(settings, store)


if __name__ == "__main__":
    main()
