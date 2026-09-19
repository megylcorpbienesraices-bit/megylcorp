"""Background WARM/COLD provider-library collector.

It expands the research library without changing the LIVE subscription set.  One asset is
processed per cycle to protect rate limits and UI responsiveness.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

from .assets import selectable_assets
from .asset_ecosystems import all_unique_equity_roots
from .provider_data_lake import DATA_LAKE
from . import alpaca_data
from .obs import note as _obs_note, expected as _obs_expected


class ProviderLibraryCollector:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._asset_index = 0
        self._component_index = 0
        self._cycles = 0
        self._last_run_at: str | None = None
        self._last_error = ""
        self._last_result: dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        return str(os.getenv("PROVIDER_LIBRARY_ENABLED", "1")).strip().lower() not in {"0", "false", "no", "off"}

    @property
    def interval_seconds(self) -> float:
        try:
            return max(20.0, min(1800.0, float(os.getenv("PROVIDER_LIBRARY_ASSET_INTERVAL_SECONDS", "75"))))
        except Exception:
            return 75.0

    @property
    def alpaca_catalog_days(self) -> int:
        try:
            return max(7, min(730, int(os.getenv("PROVIDER_LIBRARY_ALPACA_CATALOG_DAYS", "365"))))
        except Exception:
            return 365

    @property
    def alpaca_snapshot_days(self) -> int:
        try:
            return max(1, min(90, int(os.getenv("PROVIDER_LIBRARY_ALPACA_SNAPSHOT_DAYS", "30"))))
        except Exception:
            return 30

    def start(self) -> None:
        if not self.enabled or (self._task and not self._task.done()):
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="itmq-provider-library-collector")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected during orderly shutdown.
                return
            except Exception as _e:
                _obs_note('provider_library:stop', _e)
            self._task = None

    async def _run(self) -> None:
        # Give HOT startup priority.
        try:
            await asyncio.sleep(12.0)
            while not self._stop.is_set():
                try:
                    await self.collect_next_asset()
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                except asyncio.TimeoutError:
                    _obs_expected('provider_library:cadence_tick')
                    continue
        except asyncio.CancelledError:
            return

    async def collect_next_asset(self) -> dict[str, Any]:
        assets = [x for x in selectable_assets() if bool(x.get("full", False))]
        if not assets:
            return {}
        asset = assets[self._asset_index % len(assets)]
        self._asset_index += 1
        symbol = str(asset.get("symbol") or "DIA").upper()
        all_components = all_unique_equity_roots()
        component = all_components[self._component_index % len(all_components)] if all_components else symbol
        self._component_index += 1
        out: dict[str, Any] = {"symbol": symbol, "ecosystem_component": component, "alpaca": {}}

        # ALPACA COLD CATALOG: complete active contract definitions for the configured
        # research horizon. This is metadata/catalog collection, not a LIVE subscription.
        if alpaca_data.load_settings():
            try:
                catalog = await asyncio.to_thread(alpaca_data.fetch_contract_catalog, symbol, self.alpaca_catalog_days)
                rows = catalog.to_dict("records") if catalog is not None else []
                DATA_LAKE.archive_catalog(
                    source="ALPACA", symbol=symbol, catalog_type="OPTION_CONTRACT_CATALOG",
                    items=rows, metadata={"expiry_horizon_days": self.alpaca_catalog_days, "scope": "ACTIVE_CONTRACT_DEFINITIONS"},
                )
                out["alpaca"]["contract_catalog"] = len(rows)
            except Exception as exc:
                out["alpaca"]["contract_catalog_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            # A broader snapshot library is intentionally capped separately from the
            # contract-definition catalog because full OPRA snapshots are substantially heavier.
            try:
                snap = await asyncio.to_thread(alpaca_data.fetch_full_option_snapshot_library, symbol, self.alpaca_snapshot_days)
                DATA_LAKE.archive_catalog(
                    source="ALPACA", symbol=symbol, catalog_type="OPTION_SNAPSHOT",
                    items=snap, metadata={"expiry_horizon_days": self.alpaca_snapshot_days, "scope": "OPRA_SNAPSHOT_LIBRARY"},
                )
                out["alpaca"]["snapshot_contracts"] = len(snap)
            except Exception as exc:
                out["alpaca"]["snapshot_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"

            # v1.25.15 ecosystem rotation: one related ETF/equity root per WARM/COLD
            # cycle. This gradually builds the full cross-asset contract library without
            # expanding the HOT OPRA subscription set or blocking the Scanner.
            if component and component != symbol:
                try:
                    cs = await asyncio.to_thread(alpaca_data.fetch_stock_snapshot, None, component)
                    DATA_LAKE.archive_catalog(
                        source="ALPACA", symbol=component, catalog_type="ECOSYSTEM_RELATED_SNAPSHOT",
                        items=cs.get("raw") or {}, metadata={"parent_cycle_symbol": symbol, "scope": "RELATED_EQUITY_ETF"},
                    )
                    out["alpaca"]["ecosystem_snapshot"] = component
                except Exception as exc:
                    out["alpaca"]["ecosystem_snapshot_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                try:
                    cc = await asyncio.to_thread(alpaca_data.fetch_contract_catalog, component, self.alpaca_catalog_days)
                    crows = cc.to_dict("records") if cc is not None else []
                    DATA_LAKE.archive_catalog(
                        source="ALPACA", symbol=component, catalog_type="ECOSYSTEM_OPTION_CONTRACT_CATALOG",
                        items=crows, metadata={"parent_cycle_symbol": symbol, "expiry_horizon_days": self.alpaca_catalog_days},
                    )
                    out["alpaca"]["ecosystem_contract_catalog"] = {"symbol": component, "contracts": len(crows)}
                except Exception as exc:
                    out["alpaca"]["ecosystem_contract_catalog_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"


        self._cycles += 1
        self._last_run_at = datetime.now(timezone.utc).isoformat()
        self._last_result = out
        self._last_error = ""
        return out

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": bool(self._task and not self._task.done()),
            "asset_interval_seconds": self.interval_seconds,
            "alpaca_catalog_days": self.alpaca_catalog_days,
            "alpaca_snapshot_days": self.alpaca_snapshot_days,
            "cycles": self._cycles,
            "last_run_at": self._last_run_at,
            "last_error": self._last_error,
            "last_result": self._last_result,
            "policy": "ONE_ASSET_PER_CYCLE · NEVER_BLOCK_HOT_LIVE",
        }


PROVIDER_LIBRARY = ProviderLibraryCollector()
