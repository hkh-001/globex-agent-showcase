#!/usr/bin/env python3
"""Produce a deterministic, model-free landed-price transaction evidence bundle."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.application.usecases.confirmation_service import ConfirmationService
from app.application.usecases.order_usecases import OrderItemInput
from app.domain.catalog.exchange_rate import ExchangeRateTable
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.order.address import Address
from app.domain.shipping.tariff_schedule import TariffSchedule
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.persistence.sql.repositories import create_engine
from app.infrastructure.persistence.sql.trade_store import SqlTradeStore

FIXED_NOW = datetime(2026, 10, 30, 2, 30, tzinfo=timezone.utc)


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


async def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    engine = create_engine(f"sqlite+aiosqlite:///{output / 'isolated-trade.db'}")
    pricing = TariffSchedule(ExchangeRateTable())
    products = InMemoryProductRepository()
    store = SqlTradeStore(engine, clock=lambda: FIXED_NOW, tariff_schedule=pricing)
    service = ConfirmationService(
        products, store, clock=lambda: FIXED_NOW, tariff_schedule=pricing,
    )
    address = Address("验收买家", "CN", "浙江", "杭州", "测试路 1 号", "310000", "13800000000")
    try:
        await store.initialize_inventory(await products.list_all())
        products.bind_inventory(store.get_inventory)
        initial_inventory = await store.get_inventory(["P1003-S1"])
        search = await CatalogSearchUseCase(products, tariff_schedule=pricing).execute(ProductSearchSpec(
            product_id="P1003", sku_id="P1003-S1", ship_to="CN", target_currency="CNY", top_k=1,
        ))
        prepared = await service.prepare_order(
            "evidence-buyer", "evidence-session",
            [OrderItemInput("P1003", "P1003-S1", 1, "CNY", "CN")], address,
        )
        after_prepare_inventory = await store.get_inventory(["P1003-S1"])
        confirmation = prepared["confirmation"]
        created = await service.resolve(
            confirmation["confirmation_id"], "evidence-buyer", "evidence-session",
            confirmation["snapshot_hash"], True,
        )
        replay = await service.resolve(
            confirmation["confirmation_id"], "evidence-buyer", "evidence-session",
            confirmation["snapshot_hash"], True,
        )
        after_create_inventory = await store.get_inventory(["P1003-S1"])
        order = created["order"]
        cancel_prepared = await service.prepare_cancel(
            "evidence-buyer", "evidence-session", order["order_id"], "确定性验收取消",
        )
        cancel_confirmation = cancel_prepared["confirmation"]
        cancelled = await service.resolve(
            cancel_confirmation["confirmation_id"], "evidence-buyer", "evidence-session",
            cancel_confirmation["snapshot_hash"], True,
        )
        final_inventory = await store.get_inventory(["P1003-S1"])
        final_order = await store.get_order(order["order_id"], buyer_id="evidence-buyer")

        snapshots = {
            "01-search.json": search,
            "02-initial-inventory.json": initial_inventory,
            "03-create-confirmation.json": prepared,
            "04-after-prepare-inventory.json": after_prepare_inventory,
            "05-created-order.json": created,
            "06-idempotent-create-replay.json": replay,
            "07-after-create-inventory.json": after_create_inventory,
            "08-cancel-confirmation.json": cancel_prepared,
            "09-cancelled-order.json": cancelled,
            "10-final-inventory.json": final_inventory,
            "11-final-order-query.json": final_order,
        }
        for name, value in snapshots.items():
            _write(output / name, value)

        landed = search["hits"][0]["landed_price"]
        payload = confirmation["payload"]
        checks = {
            "search_landed_total_is_154_cny": landed["landed_total_major"] == 154,
            "confirmation_total_is_15400_minor": payload["total_amount_minor"] == 15400,
            "order_total_is_15400_minor": order["total_amount_minor"] == 15400,
            "pricing_breakdown_is_12900_plus_2500_plus_0": [
                payload["merchandise_subtotal_minor"], payload["freight_minor"], payload["tariff_minor"],
            ] == [12900, 2500, 0],
            "prepare_does_not_debit": initial_inventory == after_prepare_inventory == {"P1003-S1": 80},
            "approval_debits_once": after_create_inventory == {"P1003-S1": 79},
            "approval_replay_is_identical": created == replay,
            "cancel_restores_once": final_inventory == {"P1003-S1": 80},
            "cancel_preserves_price_snapshot": final_order["total_amount_minor"] == 15400,
            "final_status_is_cancelled": final_order["status"] == "CANCELLED",
        }
        if not all(checks.values()):
            raise AssertionError({key: value for key, value in checks.items() if not value})
        summary = {
            "result": "pass",
            "scope": "deterministic_local_application_loop_no_model_no_payment",
            "product_id": "P1003",
            "sku_id": "P1003-S1",
            "checks": checks,
            "model_requests": 0,
        }
        _write(output / "summary.json", summary)
        manifest = {}
        for path in sorted(output.glob("*.json")):
            manifest[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        _write(output / "manifest.json", {"sha256": manifest})
        return summary
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.output)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
