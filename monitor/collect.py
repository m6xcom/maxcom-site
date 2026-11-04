"""Hourly snapshot of stock-token collateral on Morpho, Robinhood Chain (chainId 4663) -> monitor/data.json.

Read-only: Morpho's public GraphQL API for the market list and borrower addresses, eth_call at one pinned block for
everything else (market totals, positions, oracle price and feed age), and v4quote for the cost of selling collateral
into the stock-token/USDG pools at that block.

    ROBINHOOD_RPC_URL=https://rpc.mainnet.chain.robinhood.com python monitor/collect.py
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from web3 import Web3

from v4quote.amm import uniswap_v4 as v4
from v4quote.chain.hook_fees import with_hook_fee
from v4quote.chain.loaders import load_v3_pool, load_v4_pool
from v4quote.chain.rpc import get_web3
from v4quote.routing import RouteInsufficientState, best_route

OUT = Path(__file__).resolve().parent / "data.json"
MORPHO_API = "https://blue-api.morpho.org/graphql"
MORPHO = "0x9D53d5E3bd5E8d4Cbfa6DB1ca238AEA02E651010"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
STATE_VIEW = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"
V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
GAPS = (-0.10, -0.20, -0.30)
WORDS = 48  # tick-bitmap words each side: ts 1 covers about -71 % .. +240 %

STOCK = {  # Robinhood stock tokens (18 decimals)
    "GOOGL": "0x2e0847e8910a9732eb3fb1bb4b70a580adad4fe3", "NVDA": "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec",
    "SPCX": "0x4a0e65a3eccec6dbe60ae065f2e7bb85fae35eea", "SPY": "0x117cc2133c37b721f49de2a7a74833232b3b4c0c",
    "QQQ": "0xd5f3879160bc7c32ebb4dc785f8a4f505888de68", "PLTR": "0x894e1ec2d74ffe5aef8dc8a9e84686accb964f2a",
    "USO": "0xa30fa36db767ad9ed3f7a60fc79526fb4d56d344", "MU": "0xff080c8ce2e5feadaca0da81314ae59d232d4afd",
    "AAPL": "0xaf3d76f1834a1d425780943c99ea8a608f8a93f9", "META": "0xc0d6457c16cc70d6790dd43521c899c87ce02f35",
    "TSLA": "0x322f0929c4625ed5bad873c95208d54e1c003b2d", "CRCL": "0xdf0992e440dd0be65bd8439b609d6d4366bf1cb5",
    "MSTR": "0xec262a75e413fafd0df80480274532c79d42da09", "GLD": "0xc9a981fee1f9dec688bb123ccdecc63d0debfc4e",
    "AMD": "0x86923f96303d656e4aa86d9d42d1e57ad2023fdc", "COIN": "0x6330d8c3178a418788df01a47479c0ce7ccf450b",
}
TICKER = {v: k for k, v in STOCK.items()}

VAULTS = {  # Morpho Vault V2s that lend USDG against stock tokens (checked 2026-11-04)
    "0xd0dACE70434fF7567b108B5a8c2a36C150C830FE": "Galaxy USDG RH Stock Tokens",
    "0x99347d5F70D3838763f6Bddcf80304C8aa953B57": "NetNet Credit",
    "0x8c258F25fc6fBe3f01ccC828E2C3840A46a4a9A2": "Yearn Stonk USDG",
    "0xBeEff033F34C046626B8D0A041844C5d1A5409dd": "Steakhouse USDG",
}


@dataclass(frozen=True)
class Pool:
    ticker: str
    kind: str  # v3 (Uniswap v3, Ramses V3, Slipstream: same swap math) | v4 | v4_hook (fee-only hook)
    pool: str = ""
    fee: int = 0
    ts: int = 0
    hooks: str = v4.ZERO_ADDRESS

    def key(self) -> v4.PoolKey:
        a, b = sorted([STOCK[self.ticker], USDG], key=lambda x: int(x, 16))
        return v4.PoolKey(Web3.to_checksum_address(a), Web3.to_checksum_address(b), self.fee, self.ts,
                          Web3.to_checksum_address(self.hooks))


DYN = v4.DYNAMIC_FEE_FLAG
POOLS = [  # the highest-volume USDG pools per token, verified against on-chain quoters in v4quote 0.2
    Pool("SPCX", "v3", "0xc61284332117c3fb23a2a56cceffd07f7af60029"),
    Pool("SPCX", "v4_hook", fee=DYN, ts=60, hooks="0xc15b155a3094934171a15625d1a80aba267b6080"),
    Pool("SPCX", "v4_hook", fee=DYN, ts=10, hooks="0x5eb87f69be00df39981622fd60a8de4b7837e080"),
    Pool("NVDA", "v3", "0xd4eb21209c4d6093f80b5b84f5c45cc093ea14a3"),
    Pool("NVDA", "v3", "0xdac1904d823f7d6bcaaf431ceee40c934fed321b"),
    Pool("NVDA", "v3", "0x18a5af4e442f8be68968cc1f00d537f8af2d12cd"),
    Pool("NVDA", "v4", fee=100, ts=1),
    Pool("SPY", "v4", fee=500, ts=5),
    Pool("SPY", "v3", "0x38453c115607463ac284820ce959831042f3df4e"),
    Pool("SPY", "v4", fee=100, ts=1),
    Pool("CRCL", "v3", "0x654e4143e82a5824445ade0824351c2a9acd95a8"),
    Pool("CRCL", "v4_hook", fee=DYN, ts=60, hooks="0x5eb87f69be00df39981622fd60a8de4b7837e080"),
    Pool("META", "v4", fee=3000, ts=60),
    Pool("META", "v3", "0x960f79d8dfec7f2f0c1b24cdac6bb9df1371c6e2"),
    Pool("GLD", "v3", "0xba2f1ed4ceb2169d538d1e614d847e83c5a55913"),
    Pool("GLD", "v4_hook", fee=DYN, ts=10, hooks="0xb608a78761f179f7c56f15e7d13921b92f00a080"),
    Pool("MSTR", "v4", fee=2500, ts=25),
    Pool("TSLA", "v4", fee=3000, ts=60),
    Pool("TSLA", "v3", "0xf4acdaeeb7022862a763c9b1b885e11191c889e3"),
    Pool("TSLA", "v4_hook", fee=DYN, ts=10, hooks="0x67d86050d22d574df046f3d90f722045f714e080"),
    Pool("GOOGL", "v3", "0x34d0dc122cf9a8eb296fc5e0d3a233625d7d19b7"),
    Pool("GOOGL", "v3", "0x5c0eaa89799f7b76606f4ff66c92a2654d178927"),
    Pool("AAPL", "v3", "0xaae0d815ee56e4092a5e5c2911e676fea50b2d6d"),
    Pool("AAPL", "v4", fee=3000, ts=60),
    Pool("AAPL", "v4_hook", fee=DYN, ts=10, hooks="0x70a9a88402989226847ec122043ce5e7ff462080"),
]


def gql(q: str) -> dict:
    req = urllib.request.Request(MORPHO_API, json.dumps({"query": q}).encode(),
                                 {"Content-Type": "application/json", "User-Agent": "maxcom-monitor"})
    for i in range(4):
        try:
            d = json.load(urllib.request.urlopen(req, timeout=60))
            if d.get("errors"):
                raise RuntimeError(str(d["errors"])[:300])
            return d["data"]
        except Exception:
            if i == 3:
                raise
            time.sleep(2 + 3 * i)
    raise AssertionError


def words(hexdata: bytes, n: int) -> list[int]:
    return [int.from_bytes(hexdata[i * 32:(i + 1) * 32], "big") for i in range(n)]


def lif(lltv: float) -> float:
    return min(1.15, 1 / (1 - 0.3 * (1 - lltv)))


def main() -> None:
    w3 = get_web3("robinhood")
    block = w3.eth.block_number - 5
    ts = w3.eth.get_block(block)["timestamp"]

    def call(to: str, data: str) -> bytes | None:
        for i in range(4):
            try:
                return bytes(w3.eth.call({"to": Web3.to_checksum_address(to), "data": data}, block))
            except Exception as e:
                if "revert" in str(e).lower():
                    return None
                if i == 3:
                    raise
                time.sleep(1 + i)
        return None

    # markets with stock-token collateral and a loan
    items: list[dict] = []
    for skip in range(0, 1000, 100):
        page = gql('{markets(first:100,skip:%d,where:{chainId_in:[4663]}){items{marketId lltv oracle{address type} '
                   'loanAsset{symbol address decimals} collateralAsset{symbol address decimals} '
                   'state{borrowAssetsUsd}}}}' % skip)["markets"]["items"]
        items += page
        if len(page) < 100:
            break
    markets = [m for m in items if m["collateralAsset"] and m["collateralAsset"]["address"].lower() in TICKER
               and m["loanAsset"]["address"].lower() == USDG and ((m["state"] or {}).get("borrowAssetsUsd") or 0) > 1]

    # vault allocations
    alloc: dict[str, dict[str, float]] = {}
    vaults = []
    for addr, name in VAULTS.items():
        v = gql('{vaultV2ByAddress(address:"%s",chainId:4663){totalAssetsUsd adapters{items{... on '
                'MorphoMarketV1Adapter{positions(first:100){items{state{supplyAssetsUsd} market{marketId}}}}}}}}'
                % addr)["vaultV2ByAddress"]
        stock_usd = 0.0
        for a in v["adapters"]["items"]:
            for p in (a.get("positions") or {}).get("items", []):
                usd = p["state"]["supplyAssetsUsd"] or 0
                mid = p["market"]["marketId"]
                if any(m["marketId"] == mid for m in markets) and usd > 0.5:
                    alloc.setdefault(mid, {})[name] = usd
                    stock_usd += usd
        vaults.append({"name": name, "address": addr, "tvl": v["totalAssetsUsd"], "stock_usd": stock_usd})

    # exit cost: v4quote states of the stock/USDG pools at the block
    states: dict[str, list] = {}
    for p in POOLS:
        try:
            if p.kind == "v3":
                st = load_v3_pool(w3, p.pool, block, words_each_side=WORDS, uniswap_fee_protocol=False)
            else:
                st = load_v4_pool(w3, STATE_VIEW, p.key(), block, words_each_side=WORDS)
                if p.kind == "v4_hook":
                    st = with_hook_fee(w3, POOL_MANAGER, st, sender=V4_QUOTER, block=block)
            states.setdefault(p.ticker, []).append(st)
        except Exception as e:  # a pool that cannot be loaded is left out and reported
            print(f"pool {p.ticker} {p.pool or p.hooks}: {type(e).__name__}: {str(e)[:120]}")

    def sell(ticker: str, amount: int) -> tuple[int, bool] | None:
        """USDG out for selling ``amount`` (best single pool or 2-way split); None when no pool set, and
        (0, False) when every route walks past the loaded tick range (the sale moves the price by more than that
        range, about -71 % on the finest pools)."""
        if ticker not in states or amount <= 0:
            return None
        try:
            r = best_route(STOCK[ticker], USDG, amount, states[ticker], max_hops=1, split=2, on_insufficient="report")
        except RouteInsufficientState:
            return 0, False
        return r.amount_out, r.complete

    mid_px: dict[str, float] = {}  # USDG per token from a 0.01-token sale
    for t in states:
        s = sell(t, 10**16)
        if s:
            mid_px[t] = s[0] / 1e6 / 0.01

    rows = []
    for m in markets:
        mid = m["marketId"]
        t = TICKER[m["collateralAsset"]["address"].lower()]
        lltv = int(m["lltv"]) / 1e18
        mk = call(MORPHO, "0x5c60e39a" + mid[2:])
        tsa, _, tba, tbs, _, _ = words(mk, 6)
        price = int.from_bytes(call(m["oracle"]["address"], "0xa035b1fe") or b"\0", "big")  # 0 if price() reverts
        # oracle feed age: MorphoChainlinkOracleV2 BASE_FEED_1(), custom oracle baseFeed()
        feed = None
        for sel in ("0xf50a4718", "0xfed0f855"):  # BASE_FEED_1(), baseFeed()
            r = call(m["oracle"]["address"], sel)
            if r and int.from_bytes(r, "big"):
                feed = "0x" + r[-20:].hex()
                break
        age_h = None
        if feed:
            lr = call(feed, "0xfeaf968c")  # latestRoundData()
            if lr:
                age_h = (ts - words(lr, 5)[3]) / 3600
        users, skip = [], 0
        while True:
            it = gql('{marketPositions(first:100,skip:%d,where:{chainId_in:[4663],marketUniqueKey_in:["%s"],'
                     'borrowShares_gte:1}){items{user{address}}}}' % (skip, mid))["marketPositions"]["items"]
            users += [x["user"]["address"] for x in it]
            if len(it) < 100:
                break
            skip += 100
        pos = []
        for u in users:
            r = call(MORPHO, "0x93c52062" + mid[2:] + u[2:].lower().rjust(64, "0"))
            _, bsh, col = words(r, 3)
            if bsh == 0:
                continue
            debt = -(-bsh * (tba + 1) // (tbs + 10**6)) / 1e6
            coll_usd = col * price / 1e36 / 1e6 if price else 0.0
            pos.append({"debt": debt, "coll_usd": coll_usd, "coll": col})
        debt_total = sum(p["debt"] for p in pos)
        if debt_total < 1:
            continue
        drops = [1 - p["debt"] / (p["coll_usd"] * lltv) for p in pos if p["coll_usd"] > 0 and p["debt"] >= 100]
        stress = {}
        for g in GAPS:
            liq = seized_tokens = bad = 0.0
            for p in pos:
                v2 = p["coll_usd"] * (1 + g)
                if p["debt"] > v2 * lltv:
                    rep = min(p["debt"], v2 / lif(lltv))
                    liq += rep
                    bad += p["debt"] - rep
                    seized_tokens += (p["coll"] / 1e18) * min(1.0, rep * lif(lltv) / v2) if v2 > 0 else 0
            exit_impact, exit_exact = None, True
            if seized_tokens > 0 and t in mid_px:
                s = sell(t, int(seized_tokens * 1e18))
                if s:
                    # an incomplete route (some candidate pool ran past its loaded range) still gives a lower bound
                    # of the USDG out, so the impact shown is an upper bound ("<=" on the page)
                    exit_impact = 1 - (s[0] / 1e6) / (seized_tokens * mid_px[t]) if s[0] else "beyond_range"
                    exit_exact = s[1]
            stress[str(int(-g * 100))] = {"debt_liquidatable": liq, "bad_debt": bad, "seized_tokens": seized_tokens,
                                          "exit_impact": exit_impact, "exit_exact": exit_exact}
        top = max(p["debt"] for p in pos)
        rows.append({
            "id": mid, "ticker": t, "lltv": lltv, "lif": lif(lltv),
            "oracle": m["oracle"]["address"], "oracle_type": m["oracle"]["type"], "oracle_age_h": age_h,
            "oracle_price_usd": price / 1e36 * 1e12 if price else None, "dex_price_usd": mid_px.get(t),
            "supply": tsa / 1e6, "borrow": tba / 1e6, "borrowers": len(pos), "top_borrower_share": top / debt_total,
            "first_liq_drop": min(drops) if drops else None, "stress": stress, "vaults": alloc.get(mid, {}),
            "collateral_usd": sum(p["coll_usd"] for p in pos),
        })
    rows.sort(key=lambda r: -r["borrow"])
    OUT.write_text(json.dumps({"block": block, "timestamp": ts, "generated": int(time.time()),
                               "markets": rows, "vaults": vaults,
                               "pools_loaded": {t: len(v) for t, v in states.items()}}, indent=1), encoding="utf-8")
    print(f"block {block}: {len(rows)} markets, borrow ${sum(r['borrow'] for r in rows):,.0f}")


if __name__ == "__main__":
    main()
