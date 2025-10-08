"""Sui wallet reader CLI.

This script provides read-only access to Sui wallets by querying the public
JSON-RPC endpoint. Given a wallet address it fetches the balances of all coins
held by the address and prints a readable summary.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

JSON_RPC_ID = 1

SUI_NETWORK_ENDPOINTS: Dict[str, str] = {
    "mainnet": "https://fullnode.mainnet.sui.io:443",
    "testnet": "https://fullnode.testnet.sui.io:443",
    "devnet": "https://fullnode.devnet.sui.io:443",
}

COIN_TYPE_PREFIX = "0x2::coin::Coin<"
OBJECT_PAGE_LIMIT = 5
OBJECT_PAGE_SIZE = 50
MAX_FIELD_RESULTS = 6


@dataclass
class ProtocolDefinition:
    """Definition used to identify and describe a protocol position."""

    name: str
    patterns: Sequence[str]
    preferred_fields: Sequence[str]


PROTOCOL_DEFINITIONS: Sequence[ProtocolDefinition] = (
    ProtocolDefinition(
        name="Cetus",
        patterns=("::cetus", "::clmm"),
        preferred_fields=("coin_a", "coin_b", "liquidity", "amount_a", "amount_b"),
    ),
    ProtocolDefinition(
        name="Suilend",
        patterns=("::suilend",),
        preferred_fields=("supplied", "borrowed", "collateral", "debt"),
    ),
    ProtocolDefinition(
        name="Navi Protocol",
        patterns=("::navi",),
        preferred_fields=("supplied", "borrowed", "collateral", "apy"),
    ),
    ProtocolDefinition(
        name="Bluefin",
        patterns=("::bluefin", "::perps", "::perpetual"),
        preferred_fields=("side", "size", "entry_price", "leverage"),
    ),
)


@dataclass
class ProtocolMetric:
    """Represents a labelled value describing a protocol position."""

    label: str
    value: str


@dataclass
class ProtocolPosition:
    """Represents a detected protocol position owned by an address."""

    protocol: str
    label: str
    object_id: Optional[str]
    metrics: List[ProtocolMetric]


@dataclass
class CoinBalance:
    """Represents a coin balance for a Sui address."""

    coin_type: str
    total_balance: int
    symbol: Optional[str]
    decimals: Optional[int]

    @property
    def display_symbol(self) -> str:
        if self.symbol:
            return self.symbol
        # Derive symbol from coin type last segment if metadata is missing.
        return self.coin_type.split("::")[-1]

    def formatted_balance(self) -> str:
        if self.decimals is None or self.decimals < 0:
            return str(self.total_balance)
        scaled = self.total_balance / (10 ** self.decimals)
        return f"{scaled:,.{min(self.decimals, 9)}f}"


@dataclass
class BalanceChange:
    """Represents a balance change for a specific coin type."""

    digest: str
    timestamp_ms: Optional[int]
    amount: int

    def formatted_timestamp(self) -> str:
        if self.timestamp_ms is None:
            return "unknown time"
        try:
            dt = datetime.fromtimestamp(self.timestamp_ms / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):  # pragma: no cover - platform dependent
            return "unknown time"
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


class SuiRpcError(RuntimeError):
    """Raised when the Sui JSON-RPC endpoint responds with an error."""


class SuiWalletReader:
    """Client for reading Sui wallet balances via JSON-RPC."""

    def __init__(self, network: str = "mainnet") -> None:
        if network not in SUI_NETWORK_ENDPOINTS:
            available = ", ".join(sorted(SUI_NETWORK_ENDPOINTS))
            raise ValueError(
                f"Unknown network '{network}'. Available networks: {available}"
            )
        self.network = network
        self.endpoint = SUI_NETWORK_ENDPOINTS[network]

    def read_balances(self, address: str) -> List[CoinBalance]:
        normalized = normalize_address(address)
        balances = self._call_rpc("suix_getAllBalances", [normalized])
        coins: List[CoinBalance] = []
        for entry in balances:
            coin_type = entry.get("coinType")
            raw_balance = entry.get("totalBalance")
            if coin_type is None or raw_balance is None:
                # Skip malformed entries.
                continue
            metadata = self._coin_metadata(coin_type)
            coins.append(
                CoinBalance(
                    coin_type=coin_type,
                    total_balance=int(raw_balance),
                    symbol=metadata.get("symbol") if metadata else None,
                    decimals=metadata.get("decimals") if metadata else None,
                )
            )
        return coins

    def recent_balance_changes(
        self, address: str, max_transactions: int = 50
    ) -> Dict[str, List[BalanceChange]]:
        normalized = normalize_address(address)
        if not normalized:
            return {}

        query = {
            "filter": {
                "Any": [
                    {"FromAddress": normalized},
                    {"ToAddress": normalized},
                ]
            },
            "options": {
                "showBalanceChanges": True,
                "showEffects": True,
                "showInput": False,
                "showEvents": False,
                "showObjectChanges": False,
            },
        }

        response = self._call_rpc(
            "suix_queryTransactionBlocks",
            [query, None, max_transactions, True],
        )

        transactions = []
        if isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, list):
                transactions = data

        grouped: Dict[str, List[BalanceChange]] = {}
        for tx in transactions:
            digest = tx.get("digest", "")
            timestamp_raw = tx.get("timestampMs")
            timestamp_ms = _safe_int(timestamp_raw)
            balance_changes = tx.get("balanceChanges") or []
            if not isinstance(balance_changes, list):
                continue

            for change in balance_changes:
                owner = normalize_address(_extract_owner_address(change.get("owner")))
                if not owner or owner != normalized:
                    continue

                coin_type = change.get("coinType")
                amount_raw = change.get("amount")
                if coin_type is None or amount_raw is None:
                    continue

                amount = _safe_int(amount_raw)
                if amount is None:
                    continue

                grouped.setdefault(coin_type, []).append(
                    BalanceChange(digest=digest, timestamp_ms=timestamp_ms, amount=amount)
                )

        for coin_type, entries in grouped.items():
            entries.sort(key=lambda entry: entry.timestamp_ms or 0, reverse=True)
            if len(entries) > 10:
                grouped[coin_type] = entries[:10]

        return grouped

    def protocol_positions(self, address: str) -> List[ProtocolPosition]:
        """Fetch protocol positions detected from owned objects."""

        normalized = normalize_address(address)
        if not normalized:
            return []

        positions: List[ProtocolPosition] = []
        seen_ids: set[str] = set()
        cursor: Optional[object] = None
        page = 0

        while page < OBJECT_PAGE_LIMIT:
            params = [
                normalized,
                cursor,
                OBJECT_PAGE_SIZE,
                {"showType": True, "showContent": True, "showDisplay": True},
            ]
            response = self._call_rpc("suix_getOwnedObjects", params)

            if not isinstance(response, dict):
                break

            data = response.get("data")
            objects: List[object] = data if isinstance(data, list) else []

            for entry in objects:
                if isinstance(entry, dict) and "data" in entry:
                    object_data = entry.get("data")
                else:
                    object_data = entry

                position = build_protocol_position(object_data)
                if not position:
                    continue

                if position.object_id and position.object_id in seen_ids:
                    continue

                if position.object_id:
                    seen_ids.add(position.object_id)

                positions.append(position)

            page += 1
            cursor = response.get("nextCursor")
            if not response.get("hasNextPage") or cursor in (None, {}):
                break

        return positions

    def _call_rpc(self, method: str, params: Iterable[object]) -> object:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": list(params),
                "id": JSON_RPC_ID,
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            self.endpoint, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:  # pragma: no cover - network errors
            raise ConnectionError(
                f"Failed to connect to Sui endpoint '{self.endpoint}': {exc}"
            ) from exc

        if "error" in data:
            error = data["error"]
            message = error.get("message", "Unknown error")
            raise SuiRpcError(f"RPC error calling {method}: {message}")
        return data.get("result")

    @lru_cache(maxsize=128)
    def _coin_metadata(self, coin_type: str) -> Dict[str, object]:
        try:
            metadata = self._call_rpc("suix_getCoinMetadata", [coin_type])
        except SuiRpcError:
            # Some coins may not have metadata. Return empty dict to avoid noisy output.
            return {}
        if metadata is None:
            return {}
        return metadata


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read holdings for a Sui wallet.")
    parser.add_argument(
        "address",
        help="Sui wallet address (0x...) to inspect",
    )
    parser.add_argument(
        "--network",
        default="mainnet",
        choices=sorted(SUI_NETWORK_ENDPOINTS),
        help="Sui network to query (default: mainnet)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    reader = SuiWalletReader(network=args.network)
    try:
        balances = reader.read_balances(args.address)
        balance_changes = reader.recent_balance_changes(args.address)
        protocol_positions = reader.protocol_positions(args.address)
    except (ConnectionError, SuiRpcError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not balances:
        print("No balances found for this address.")
        return 0

    print(f"Holdings for address {args.address} on {args.network}:")
    print("-" * 60)
    for coin in balances:
        amount = coin.formatted_balance()
        symbol = coin.display_symbol
        print(f"{symbol:<20} {amount:>20}  ({coin.coin_type})")

        changes = balance_changes.get(coin.coin_type, [])
        if not changes:
            print("    No recent balance changes tracked.")
            continue

        for change in changes:
            formatted_amount = format_amount(change.amount, coin.decimals)
            direction = "received" if change.amount > 0 else "sent"
            timestamp = change.formatted_timestamp()
            print(
                f"    {timestamp} · {formatted_amount} {symbol} {direction} · tx {change.digest}"
            )
    print("-" * 60)
    print(f"Total coins: {len(balances)}")

    print()
    if protocol_positions:
        print("Protocol positions:")
        print("-" * 60)
        for position in sorted(protocol_positions, key=lambda pos: pos.protocol):
            label = position.label or "Unlabelled position"
            print(f"{position.protocol}: {label}")
            for metric in position.metrics:
                print(f"    {metric.label}: {metric.value}")
            if position.object_id:
                print(f"    Object ID: {position.object_id}")
            print("-" * 40)
    else:
        print("No protocol positions detected for this address.")

    return 0


def _safe_int(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_address(raw: Optional[str]) -> str:
    if not raw:
        return ""
    trimmed = raw.strip().lower()
    if not trimmed:
        return ""
    return trimmed if trimmed.startswith("0x") else f"0x{trimmed}"


def _extract_owner_address(owner: object) -> Optional[str]:
    if owner is None:
        return None
    if isinstance(owner, str):
        return owner
    if isinstance(owner, dict):
        for key in ("AddressOwner", "GasOwner", "ObjectOwner"):
            nested = owner.get(key)
            if isinstance(nested, str):
                return nested
    return None


def format_amount(amount: int, decimals: Optional[int]) -> str:
    sign = "" if amount >= 0 else "-"
    absolute = abs(amount)
    if decimals is None or decimals <= 0:
        return f"{sign}{absolute}"
    scaled = absolute / (10 ** decimals)
    if decimals > 9:
        decimals = 9
    formatted = f"{scaled:,.{decimals}f}".rstrip("0").rstrip(".")
    return f"{sign}{formatted}"


def to_title_case(value: str) -> str:
    return " ".join(segment.capitalize() for segment in value.replace("_", " ").replace("-", " ").split())


def value_to_string(value: object) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, (int, float)):
        return str(value)

    if isinstance(value, str):
        text = value.strip()
        return text or None

    if isinstance(value, list):
        formatted = [value_to_string(item) for item in value]
        compact = [item for item in formatted if item]
        return ", ".join(compact) if compact else None

    if isinstance(value, dict):
        if isinstance(value.get("id"), str):
            return value["id"]
        if isinstance(value.get("objectId"), str):
            return value["objectId"]
        if "fields" in value:
            return value_to_string(value.get("fields"))

        entries = []
        for key, nested in value.items():
            if len(entries) >= 2:
                break
            formatted = value_to_string(nested)
            if formatted:
                entries.append(f"{to_title_case(key)}: {formatted}")
        return " • ".join(entries) if entries else None

    return str(value)


def extract_display_label(display: object) -> Optional[str]:
    if not isinstance(display, dict):
        return None

    data = display.get("data") if isinstance(display.get("data"), dict) else display
    if not isinstance(data, dict):
        return None

    for key in ("name", "title", "label"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def create_display_entries(display: object) -> List[ProtocolMetric]:
    if not isinstance(display, dict):
        return []

    data = display.get("data") if isinstance(display.get("data"), dict) else display
    if not isinstance(data, dict):
        return []

    metrics: List[ProtocolMetric] = []
    for key, value in data.items():
        if key in {"name", "title", "label"}:
            continue
        formatted = value_to_string(value)
        if not formatted:
            continue
        metrics.append(ProtocolMetric(label=to_title_case(key), value=formatted))
        if len(metrics) >= MAX_FIELD_RESULTS:
            break
    return metrics


def shorten_type_name(value: str) -> str:
    without_generics = value.split("<", 1)[0]
    parts = without_generics.split("::")
    return parts[-1] if parts else value


def find_field_value(fields: object, target_key: str) -> Optional[object]:
    if isinstance(fields, dict):
        if target_key in fields:
            return fields[target_key]
        for nested in fields.values():
            result = find_field_value(nested, target_key)
            if result is not None:
                return result
    elif isinstance(fields, list):
        for item in fields:
            result = find_field_value(item, target_key)
            if result is not None:
                return result
    return None


def collect_field_entries(
    fields: object, definition: Optional[ProtocolDefinition]
) -> List[ProtocolMetric]:
    if not isinstance(fields, dict):
        return []

    metrics: List[ProtocolMetric] = []
    seen: set[Tuple[str, str]] = set()

    def add_metric(label: str, value: str) -> None:
        key = (label.lower(), value)
        if key in seen:
            return
        seen.add(key)
        metrics.append(ProtocolMetric(label=label, value=value))

    preferred = definition.preferred_fields if definition else []
    for key in preferred:
        formatted = value_to_string(find_field_value(fields, key))
        if not formatted:
            continue
        add_metric(to_title_case(key), formatted)
        if len(metrics) >= MAX_FIELD_RESULTS:
            return metrics

    for key, value in fields.items():
        formatted = value_to_string(value)
        if not formatted:
            continue
        add_metric(to_title_case(key), formatted)
        if len(metrics) >= MAX_FIELD_RESULTS:
            break

    return metrics


def derive_protocol_label(
    definition: Optional[ProtocolDefinition], fields: object, type_name: str
) -> str:
    if isinstance(fields, dict):
        coin_a = find_field_value(fields, "coin_a") or find_field_value(fields, "coinA") or find_field_value(fields, "token_a")
        coin_b = find_field_value(fields, "coin_b") or find_field_value(fields, "coinB") or find_field_value(fields, "token_b")
        if coin_a and coin_b:
            formatted_a = value_to_string(coin_a) or shorten_type_name(str(coin_a))
            formatted_b = value_to_string(coin_b) or shorten_type_name(str(coin_b))
            if formatted_a and formatted_b:
                return f"{shorten_type_name(formatted_a)} / {shorten_type_name(formatted_b)}"

        market = find_field_value(fields, "market") or find_field_value(fields, "pool_id") or find_field_value(fields, "pool")
        if market:
            formatted_market = value_to_string(market)
            if formatted_market:
                return formatted_market

        asset = find_field_value(fields, "asset") or find_field_value(fields, "coin_type") or find_field_value(fields, "reserve")
        if asset:
            formatted_asset = value_to_string(asset)
            if formatted_asset:
                return formatted_asset

    if definition:
        return f"{definition.name} position"
    return shorten_type_name(type_name)


def identify_protocol_by_type(type_name: str) -> Optional[ProtocolDefinition]:
    lower = type_name.lower()
    for definition in PROTOCOL_DEFINITIONS:
        if any(pattern in lower for pattern in definition.patterns):
            return definition
    return None


def build_protocol_position(object_data: object) -> Optional[ProtocolPosition]:
    if not isinstance(object_data, dict):
        return None

    type_name = (
        object_data.get("type")
        or (object_data.get("content") or {}).get("type")
        or ""
    )
    if not type_name or type_name.startswith(COIN_TYPE_PREFIX):
        return None

    definition = identify_protocol_by_type(type_name)
    if not definition:
        return None

    object_id = (
        object_data.get("objectId")
        or (object_data.get("reference") or {}).get("objectId")
        or ((object_data.get("content") or {}).get("fields") or {}).get("id")
    )
    if isinstance(object_id, dict):
        object_id = value_to_string(object_id)

    display = object_data.get("display")
    display_metrics = create_display_entries(display)
    display_label = extract_display_label(display)

    fields = (
        (object_data.get("content") or {}).get("fields")
        or object_data.get("fields")
        or None
    )
    field_metrics = collect_field_entries(fields, definition)

    metrics: List[ProtocolMetric] = []
    seen: set[Tuple[str, str]] = set()
    for collection in (display_metrics, field_metrics):
        for metric in collection:
            key = (metric.label.lower(), metric.value)
            if key in seen:
                continue
            seen.add(key)
            metrics.append(metric)
            if len(metrics) >= MAX_FIELD_RESULTS:
                break
        if len(metrics) >= MAX_FIELD_RESULTS:
            break

    label = display_label or derive_protocol_label(definition, fields, type_name)

    return ProtocolPosition(
        protocol=definition.name,
        label=label,
        object_id=object_id if isinstance(object_id, str) else None,
        metrics=metrics,
    )


if __name__ == "__main__":
    sys.exit(main())
