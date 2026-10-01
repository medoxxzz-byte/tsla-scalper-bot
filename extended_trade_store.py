"""Immutable-ledger helpers for the Dashboard-only extended Paper trade.

This module deliberately does not touch tm_research_events, V17/V18 tracking,
or the /manual game journal. It persists a separate one-trade-per-New-York-day
Paper-only experiment in Neon.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover
    psycopg = None
    dict_row = None
    Jsonb = None

LOGGER = logging.getLogger(__name__)
TRADE_TABLE = "extended_paper_trades"
EVENT_TABLE = "extended_paper_trade_events"
DECISION_VERSION = "extended-paper-v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _event_key(trade_key: str, event_type: str) -> str:
    material = {"trade_key": trade_key, "event_type": event_type, "decision_version": DECISION_VERSION}
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class ExtendedPaperTradeStore:
    """Append-only event ledger plus isolated state summary for extended Paper trades."""

    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = database_url or os.environ.get("DATABASE_URL", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.database_url and psycopg is not None and Jsonb is not None)

    def healthcheck(self) -> Dict[str, Any]:
        if not self.configured:
            return {"status": "unavailable", "decision_version": DECISION_VERSION, "detail": "Neon store unavailable"}
        try:
            with psycopg.connect(self.database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT 1 FROM {TRADE_TABLE} LIMIT 1")
            return {"status": "ok", "decision_version": DECISION_VERSION}
        except Exception as exc:
            LOGGER.warning("[ExtendedPaper] Healthcheck failed: %s", exc)
            return {"status": "error", "decision_version": DECISION_VERSION, "detail": "Neon store unavailable"}

    def _connect(self):
        if not self.configured:
            raise RuntimeError("Extended Paper Neon store is not configured")
        return psycopg.connect(self.database_url, connect_timeout=5, row_factory=dict_row)

    @staticmethod
    def _serialize(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        result = dict(row)
        for key, value in list(result.items()):
            if isinstance(value, datetime):
                result[key] = value.isoformat()
            elif isinstance(value, date):
                result[key] = value.isoformat()
            elif hasattr(value, "as_dict"):
                result[key] = value.as_dict()
        return result

    def get_trade_for_date(self, trade_date: date) -> Optional[Dict[str, Any]]:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT * FROM {TRADE_TABLE} WHERE trade_date = %s",
                        (trade_date,),
                    )
                    return self._serialize(cursor.fetchone())
        except Exception as exc:
            LOGGER.error("[ExtendedPaper] Date lookup failed: %s", exc)
            return None

    def get_open_trade(self) -> Optional[Dict[str, Any]]:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT * FROM {TRADE_TABLE}
                        WHERE status = 'open'
                        ORDER BY decision_at DESC
                        LIMIT 1
                        """
                    )
                    return self._serialize(cursor.fetchone())
        except Exception as exc:
            LOGGER.error("[ExtendedPaper] Open lookup failed: %s", exc)
            return None

    def latest_trade(self) -> Optional[Dict[str, Any]]:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT * FROM {TRADE_TABLE} ORDER BY decision_at DESC LIMIT 1")
                    return self._serialize(cursor.fetchone())
        except Exception as exc:
            LOGGER.error("[ExtendedPaper] Latest lookup failed: %s", exc)
            return None

    def reserve_decision(self, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Atomically reserve the sole extended trade for a New York date."""
        trade_date = payload["trade_date"]
        trade_key = payload["trade_key"]
        decision_at = payload["decision_at"]
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        INSERT INTO {TRADE_TABLE} (
                            trade_date, trade_key, status, direction, decision_version,
                            execution_environment, decision_at, tsla_decision_price,
                            vwap_at_decision, invalidation_price, target_030_price,
                            target_060_price, target_120_price, decision_context
                        ) VALUES (
                            %s, %s, 'reserved', %s, %s, 'alpaca_paper', %s, %s,
                            %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (trade_date) DO NOTHING
                        RETURNING *
                        """,
                        (
                            trade_date, trade_key, payload["direction"], DECISION_VERSION,
                            decision_at, payload["tsla_price"], payload["vwap"],
                            payload["invalidation_price"], payload["target_030_price"],
                            payload["target_060_price"], payload["target_120_price"],
                            Jsonb(payload["decision_context"]),
                        ),
                    )
                    inserted = cursor.fetchone()
                    if inserted:
                        row = dict(inserted)
                        cursor.execute(
                            f"""
                            INSERT INTO {EVENT_TABLE} (trade_id, event_key, event_type, observed_at, tsla_price, payload)
                            VALUES (%s, %s, 'decision_reserved', %s, %s, %s)
                            ON CONFLICT (event_key) DO NOTHING
                            """,
                            (
                                row["id"], _event_key(trade_key, "decision_reserved"), decision_at,
                                payload["tsla_price"], Jsonb(payload["decision_context"]),
                            ),
                        )
                        return self._serialize(row), True
                    cursor.execute(f"SELECT * FROM {TRADE_TABLE} WHERE trade_date = %s", (trade_date,))
                    return self._serialize(cursor.fetchone()), False
        except Exception as exc:
            LOGGER.error("[ExtendedPaper] Decision reservation failed: %s", exc)
            return None, False

    def mark_entry_opened(self, trade_id: int, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        UPDATE {TRADE_TABLE}
                        SET status = 'open', entered_at = %s, option_symbol = %s,
                            option_expiry = %s, option_strike = %s, option_entry_mid = %s,
                            option_quantity = 1, alpaca_entry_order_id = %s,
                            tsla_entry_price = %s, updated_at = NOW()
                        WHERE id = %s AND status = 'reserved'
                        RETURNING *
                        """,
                        (
                            fields["entered_at"], fields["option_symbol"], fields["option_expiry"],
                            fields["option_strike"], fields["option_entry_mid"], fields["alpaca_entry_order_id"],
                            fields["tsla_entry_price"], trade_id,
                        ),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return None
                    row = dict(row)
                    cursor.execute(
                        f"""
                        INSERT INTO {EVENT_TABLE} (trade_id, event_key, event_type, observed_at, tsla_price, option_mid, payload)
                        VALUES (%s, %s, 'entry_opened', %s, %s, %s, %s)
                        ON CONFLICT (event_key) DO NOTHING
                        """,
                        (
                            trade_id, _event_key(row["trade_key"], "entry_opened"), fields["entered_at"],
                            fields["tsla_entry_price"], fields["option_entry_mid"], Jsonb(fields["event_payload"]),
                        ),
                    )
                    return self._serialize(row)
        except Exception as exc:
            LOGGER.error("[ExtendedPaper] Mark entry opened failed: %s", exc)
            return None

    def mark_entry_failed(self, trade_id: int, observed_at: datetime, tsla_price: float, detail: Dict[str, Any]) -> None:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        UPDATE {TRADE_TABLE}
                        SET status = 'entry_failed', exit_reason = 'entry_failed', updated_at = NOW()
                        WHERE id = %s AND status = 'reserved'
                        RETURNING trade_key
                        """,
                        (trade_id,),
                    )
                    row = cursor.fetchone()
                    if row:
                        cursor.execute(
                            f"""
                            INSERT INTO {EVENT_TABLE} (trade_id, event_key, event_type, observed_at, tsla_price, payload)
                            VALUES (%s, %s, 'entry_failed', %s, %s, %s)
                            ON CONFLICT (event_key) DO NOTHING
                            """,
                            (trade_id, _event_key(row["trade_key"], "entry_failed"), observed_at, tsla_price, Jsonb(detail)),
                        )
        except Exception as exc:
            LOGGER.error("[ExtendedPaper] Mark entry failed failed: %s", exc)

    def record_progress(
        self,
        trade_id: int,
        observed_at: datetime,
        tsla_price: float,
        option_mid: Optional[float],
        favorable: float,
        adverse: float,
    ) -> Tuple[Optional[Dict[str, Any]], Iterable[str]]:
        """Update extrema and atomically return only newly reached milestones."""
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT * FROM {TRADE_TABLE} WHERE id = %s FOR UPDATE", (trade_id,))
                    row = cursor.fetchone()
                    if not row or row["status"] != "open":
                        return self._serialize(row), []
                    row = dict(row)
                    milestones = []
                    hit_updates = []
                    for amount, name, column in ((0.30, "target_030_hit", "hit_030_at"), (0.60, "target_060_hit", "hit_060_at"), (1.20, "target_120_hit", "hit_120_at")):
                        if favorable >= amount and row.get(column) is None:
                            hit_updates.append(column)
                            milestones.append(name)
                    assignments = [
                        "max_favorable_extension = GREATEST(max_favorable_extension, %s)",
                        "max_adverse_extension = GREATEST(max_adverse_extension, %s)",
                        "updated_at = NOW()",
                    ]
                    params = [favorable, adverse]
                    for column in hit_updates:
                        assignments.append(f"{column} = %s")
                        params.append(observed_at)
                    params.append(trade_id)
                    cursor.execute(
                        f"UPDATE {TRADE_TABLE} SET {', '.join(assignments)} WHERE id = %s RETURNING *",
                        params,
                    )
                    updated = dict(cursor.fetchone())
                    for event_type in milestones:
                        cursor.execute(
                            f"""
                            INSERT INTO {EVENT_TABLE} (trade_id, event_key, event_type, observed_at, tsla_price, option_mid, payload)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (event_key) DO NOTHING
                            """,
                            (
                                trade_id, _event_key(updated["trade_key"], event_type), event_type, observed_at,
                                tsla_price, option_mid, Jsonb({"favorable_extension": favorable, "adverse_extension": adverse}),
                            ),
                        )
                    return self._serialize(updated), milestones
        except Exception as exc:
            LOGGER.error("[ExtendedPaper] Progress update failed: %s", exc)
            return None, []

    def close_trade(self, trade_id: int, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        UPDATE {TRADE_TABLE}
                        SET status = 'closed', closed_at = %s, option_exit_mid = %s,
                            alpaca_exit_order_id = %s, tsla_exit_price = %s,
                            exit_reason = %s, option_pnl_dollars = %s, updated_at = NOW()
                        WHERE id = %s AND status = 'open'
                        RETURNING *
                        """,
                        (
                            fields["closed_at"], fields["option_exit_mid"], fields["alpaca_exit_order_id"],
                            fields["tsla_exit_price"], fields["exit_reason"], fields["option_pnl_dollars"], trade_id,
                        ),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return None
                    row = dict(row)
                    cursor.execute(
                        f"""
                        INSERT INTO {EVENT_TABLE} (trade_id, event_key, event_type, observed_at, tsla_price, option_mid, payload)
                        VALUES (%s, %s, 'exit_closed', %s, %s, %s, %s)
                        ON CONFLICT (event_key) DO NOTHING
                        """,
                        (
                            trade_id, _event_key(row["trade_key"], "exit_closed"), fields["closed_at"],
                            fields["tsla_exit_price"], fields["option_exit_mid"], Jsonb({"exit_reason": fields["exit_reason"], "option_pnl_dollars": fields["option_pnl_dollars"]}),
                        ),
                    )
                    return self._serialize(row)
        except Exception as exc:
            LOGGER.error("[ExtendedPaper] Close update failed: %s", exc)
            return None

    def record_exit_failure(self, trade_id: int, observed_at: datetime, tsla_price: float, detail: Dict[str, Any]) -> None:
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT trade_key FROM {TRADE_TABLE} WHERE id = %s", (trade_id,))
                    row = cursor.fetchone()
                    if row:
                        cursor.execute(
                            f"""
                            INSERT INTO {EVENT_TABLE} (trade_id, event_key, event_type, observed_at, tsla_price, payload)
                            VALUES (%s, %s, 'exit_failed', %s, %s, %s)
                            ON CONFLICT (event_key) DO NOTHING
                            """,
                            (trade_id, _event_key(row["trade_key"], "exit_failed"), observed_at, tsla_price, Jsonb(detail)),
                        )
        except Exception as exc:
            LOGGER.error("[ExtendedPaper] Exit failure event failed: %s", exc)
