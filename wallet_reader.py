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
        balances = self._call_rpc("suix_getAllBalances", [address])
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
    print("-" * 60)
    print(f"Total coins: {len(balances)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
