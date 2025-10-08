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
from typing import Dict, Iterable, List, Optional

JSON_RPC_ID = 1

SUI_NETWORK_ENDPOINTS: Dict[str, str] = {
    "mainnet": "https://fullnode.mainnet.sui.io:443",
    "testnet": "https://fullnode.testnet.sui.io:443",
    "devnet": "https://fullnode.devnet.sui.io:443",
}


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


if __name__ == "__main__":
    sys.exit(main())
