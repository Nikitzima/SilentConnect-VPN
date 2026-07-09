from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class XuiDatabase:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        db_uri = self.path.as_posix()
        conn = sqlite3.connect(f"file:{db_uri}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _parse_settings(raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Unexpected x-ui JSON structure")
        return parsed

    def find_client_by_email(self, email: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, remark, protocol, port, settings, stream_settings, sniffing
                FROM inbounds
                ORDER BY id
                """
            ).fetchall()
        for row in rows:
            settings = self._parse_settings(row["settings"])
            for client in settings.get("clients") or []:
                if client.get("email") == email:
                    return {
                        "inbound_id": row["id"],
                        "remark": row["remark"],
                        "protocol": row["protocol"],
                        "port": row["port"],
                        "client": client,
                        "settings": settings,
                        "stream_settings": self._parse_settings(row["stream_settings"]),
                        "sniffing": self._parse_settings(row["sniffing"]),
                    }
        return None

    def find_client_by_sub_id(self, sub_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, remark, protocol, port, settings, stream_settings, sniffing
                FROM inbounds
                ORDER BY id
                """
            ).fetchall()
        for row in rows:
            settings = self._parse_settings(row["settings"])
            for client in settings.get("clients") or []:
                if client.get("subId") == sub_id:
                    return {
                        "inbound_id": row["id"],
                        "remark": row["remark"],
                        "protocol": row["protocol"],
                        "port": row["port"],
                        "client": client,
                        "settings": settings,
                        "stream_settings": self._parse_settings(row["stream_settings"]),
                        "sniffing": self._parse_settings(row["sniffing"]),
                    }
        return None

    def get_client_traffic(self, email: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT inbound_id, email, up, down, total, expiry_time, enable, last_online
                FROM client_traffics
                WHERE email = ?
                """,
                (email,),
            ).fetchone()
        return dict(row) if row else None

    def list_client_traffic(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT inbound_id, email, up, down, total, expiry_time, enable, last_online
                FROM client_traffics
                ORDER BY (COALESCE(up, 0) + COALESCE(down, 0)) DESC
                LIMIT ?
                """,
                (max(int(limit), 1),),
            ).fetchall()
        return [dict(row) for row in rows]
