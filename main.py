import asyncio
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

DEX = "https://api.dexscreener.com"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
APP_VERSION = "0.4.0"
RPC_TIMEOUT = 20.0

app = FastAPI(title="Solana AI Bot API", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def valid_address(address: str) -> bool:
    return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address))


async def rpc_call(client: httpx.AsyncClient, method: str, params: list) -> dict:
    response = await client.post(
        SOLANA_RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(payload["error"].get("message", "Solana RPC error"))
    return payload.get("result") or {}


def analyze_pair(pair: dict, address: str) -> dict | None:
    base = pair.get("baseToken") or {}
    if str(base.get("address", "")).lower() != address.lower():
        return None

    liquidity = num((pair.get("liquidity") or {}).get("usd"))
    volume = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    change = pair.get("priceChange") or {}
    h24 = txns.get("h24") or {}
    h6 = txns.get("h6") or {}
    h1 = txns.get("h1") or {}
    m5 = txns.get("m5") or {}

    buys = int(num(h24.get("buys")))
    sells = int(num(h24.get("sells")))
    total = buys + sells
    price_change = num(change.get("h24"))

    score = 0
    score += 30 if liquidity >= 50000 else 20 if liquidity >= 10000 else 10 if liquidity >= 3000 else 0
    score += 25 if num(volume.get("h24")) >= 100000 else 15 if num(volume.get("h24")) >= 20000 else 8 if num(volume.get("h24")) >= 5000 else 0
    score += 20 if total >= 500 else 12 if total >= 100 else 5 if total >= 25 else 0
    score += min(15, int(max(0, price_change) / 2))

    risks = []
    if liquidity < 3000:
        risks.append("Very low liquidity")
    if total < 10:
        risks.append("Very low trading activity")
    if sells > buys * 2 and sells >= 20:
        risks.append("Selling activity significantly exceeds buying")
    if price_change <= -30:
        risks.append("Price dropped more than 30% in 24 hours")
    if liquidity > 0 and num(volume.get("h24")) > liquidity * 10:
        risks.append("Unusually high trading volume relative to liquidity")

    created = pair.get("pairCreatedAt")
    age_hours = None
    if created:
        age_hours = max(
            0,
            (datetime.now(timezone.utc).timestamp() * 1000 - num(created)) / 3600000,
        )

    return {
        "token": {
            "address": address,
            "name": base.get("name"),
            "symbol": base.get("symbol"),
            "price_usd": pair.get("priceUsd"),
            "market_cap": pair.get("marketCap"),
            "fdv": pair.get("fdv"),
        },
        "liquidity": {"usd": liquidity},
        "volume": {
            "5m": num(volume.get("m5")),
            "1h": num(volume.get("h1")),
            "6h": num(volume.get("h6")),
            "24h": num(volume.get("h24")),
        },
        "transactions": {
            "5m_buys": int(num(m5.get("buys"))),
            "5m_sells": int(num(m5.get("sells"))),
            "1h_buys": int(num(h1.get("buys"))),
            "1h_sells": int(num(h1.get("sells"))),
            "6h_buys": int(num(h6.get("buys"))),
            "6h_sells": int(num(h6.get("sells"))),
            "24h_buys": buys,
            "24h_sells": sells,
            "24h_total": total,
        },
        "price_change_percent": {
            "5m": num(change.get("m5")),
            "1h": num(change.get("h1")),
            "6h": num(change.get("h6")),
            "24h": price_change,
        },
        "pair": {
            "dex": pair.get("dexId"),
            "address": pair.get("pairAddress"),
            "url": pair.get("url"),
            "created_at": created,
            "age_hours": round(age_hours, 2) if age_hours is not None else None,
        },
        "screening": {
            "score": score,
            "risks": risks,
            "signal": "AVOID" if liquidity < 3000 or total < 10 else "WATCH" if score >= 55 else "REVIEW",
        },
        "limitations": [
            "Market-data screening is not a complete security audit.",
            "See the separate on-chain security section for checks that were available.",
            "A low risk score does not guarantee safety or profit.",
        ],
    }


async def inspect_token_security(client: httpx.AsyncClient, address: str) -> dict:
    """Read mint metadata and largest token accounts from Solana RPC."""
    result = {
        "status": "partial",
        "source": SOLANA_RPC,
        "mint_authority": {"status": "unavailable", "value": None},
        "freeze_authority": {"status": "unavailable", "value": None},
        "token_program": None,
        "supply": None,
        "largest_holders": {
            "status": "unavailable",
            "accounts_checked": 0,
            "unique_owners_found": 0,
            "top_10_owner_share_percent": None,
            "top_20_owner_share_percent": None,
            "note": "Holder concentration is estimated from the largest token accounts; unavailable owners are not counted.",
        },
        "extensions": {"status": "not_confirmed", "items": []},
        "risks": [],
        "errors": [],
    }

    try:
        mint_result = await rpc_call(
            client,
            "getAccountInfo",
            [address, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        value = mint_result.get("value")
        if not value:
            result["errors"].append("Mint account was not found by Solana RPC.")
            return result

        result["token_program"] = value.get("owner")
        parsed = (((value.get("data") or {}).get("parsed") or {}).get("info") or {})
        if parsed:
            mint_auth = parsed.get("mintAuthority")
            freeze_auth = parsed.get("freezeAuthority")
            result["mint_authority"] = {
                "status": "present" if mint_auth else "revoked",
                "value": mint_auth,
            }
            result["freeze_authority"] = {
                "status": "present" if freeze_auth else "revoked",
                "value": freeze_auth,
            }
            if mint_auth:
                result["risks"].append("Mint authority is still enabled; additional tokens may be minted.")
            if freeze_auth:
                result["risks"].append("Freeze authority is still enabled; token accounts may be frozen.")
            extensions = parsed.get("extensions")
            if isinstance(extensions, list):
                result["extensions"] = {
                    "status": "reported_by_rpc",
                    "items": [
                        item.get("extension", str(item))
                        if isinstance(item, dict) else str(item)
                        for item in extensions
                    ],
                }
            else:
                result["extensions"] = {
                    "status": "not_returned_by_rpc",
                    "items": [],
                }
        else:
            result["errors"].append(
                "RPC did not return parsed mint data; authority fields could not be verified."
            )

    except Exception as exc:
        result["errors"].append(f"Mint metadata check failed: {type(exc).__name__}")

    try:
        supply_result = await rpc_call(
            client, "getTokenSupply", [address, {"commitment": "confirmed"}]
        )
        supply_info = supply_result.get("value") or {}
        result["supply"] = {
            "amount": supply_info.get("uiAmountString"),
            "decimals": supply_info.get("decimals"),
            "raw_amount": supply_info.get("amount"),
        }
        total_supply = num(supply_info.get("amount"))
        decimals = int(num(supply_info.get("decimals")))
        divisor = 10 ** decimals
    except Exception as exc:
        total_supply = 0
        divisor = 1
        result["errors"].append(f"Token supply check failed: {type(exc).__name__}")

    try:
        largest_result = await rpc_call(
            client,
            "getTokenLargestAccounts",
            [address, {"commitment": "confirmed"}],
        )
        accounts = (largest_result.get("value") or [])[:20]
        result["largest_holders"]["accounts_checked"] = len(accounts)

        account_addresses = [item.get("address") for item in accounts if item.get("address")]
        if account_addresses:
            owners_result = await rpc_call(
                client,
                "getMultipleAccounts",
                [account_addresses, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            )
            values = owners_result.get("value") or []
            owner_amounts = {}
            for account, account_value in zip(accounts, values):
                if not account_value:
                    continue
                info = (
                    (((account_value.get("data") or {}).get("parsed") or {}).get("info"))
                    or {}
                )
                owner = info.get("owner")
                token_amount = info.get("tokenAmount") or {}
                raw_amount = num(token_amount.get("amount", account.get("amount")))
                if owner:
                    owner_amounts[owner] = owner_amounts.get(owner, 0) + raw_amount

            if owner_amounts and total_supply > 0:
                ranked = sorted(owner_amounts.values(), reverse=True)
                top10 = sum(ranked[:10]) / total_supply * 100
                top20 = sum(ranked[:20]) / total_supply * 100
                result["largest_holders"].update({
                    "status": "ok",
                    "unique_owners_found": len(owner_amounts),
                    "top_10_owner_share_percent": round(top10, 4),
                    "top_20_owner_share_percent": round(top20, 4),
                })
                if top10 >= 50:
                    result["risks"].append("The top 10 identified owners hold at least 50% of total supply.")
                elif top10 >= 25:
                    result["risks"].append("The top 10 identified owners hold at least 25% of total supply.")
            else:
                result["errors"].append(
                    "Largest token accounts were found, but owner concentration could not be calculated."
                )
    except Exception as exc:
        result["errors"].append(f"Largest holder check failed: {type(exc).__name__}")

    if (
        result["mint_authority"]["status"] != "unavailable"
        and result["freeze_authority"]["status"] != "unavailable"
        and result["largest_holders"]["status"] == "ok"
    ):
        result["status"] = "ok"

    result["limitations"] = [
        "Largest token accounts are not the same as a complete list of beneficial holders.",
        "The RPC provider may rate-limit requests or omit parsed Token-2022 extension details.",
        "This check does not verify LP locks, creator-linked wallets, snipers, wash trading, or all Token-2022 risks.",
        "This is an automated screening result, not a guarantee that a token is safe.",
    ]
    return result


async def get_pairs(client: httpx.AsyncClient, address: str) -> list:
    response = await client.get(f"{DEX}/token-pairs/v1/solana/{address}")
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


@app.get("/")
async def root():
    return {
        "service": "Solana AI Bot API",
        "status": "ok",
        "mode": "paper",
        "version": APP_VERSION,
        "health": "/api/health",
        "tokens": "/api/tokens",
        "analyze": "/api/analyze/{token_address}",
        "trading_status": "/api/trading/status",
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "mode": "paper", "version": APP_VERSION}


@app.get("/api/trading/status")
async def trading_status():
    return {
        "status": "not_started",
        "mode": "paper",
        "live_trading_enabled": False,
        "wallet_connected": False,
        "message": "Trading execution is not enabled. No wallet or real funds are connected.",
        "next_steps": [
            "Build and test a paper-trading strategy.",
            "Add persistent trade and balance storage.",
            "Add wallet signing and risk controls only after explicit setup.",
        ],
    }


@app.get("/api/security/{address}")
async def security_check(address: str):
    if not valid_address(address):
        raise HTTPException(status_code=400, detail="Invalid Solana token address")
    async with httpx.AsyncClient(timeout=RPC_TIMEOUT) as client:
        report = await inspect_token_security(client, address)
    return {
        "status": "ok",
        "chain": "solana",
        "address": address,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "security": report,
    }


@app.get("/api/analyze/{address}")
async def analyze_token(address: str):
    if not valid_address(address):
        raise HTTPException(status_code=400, detail="Invalid Solana token address")

    try:
        async with httpx.AsyncClient(timeout=RPC_TIMEOUT) as client:
            pairs_task = get_pairs(client, address)
            security_task = inspect_token_security(client, address)
            pairs, security = await asyncio.gather(pairs_task, security_task)

        reports = [
            report
            for pair in pairs
            if (report := analyze_pair(pair, address)) is not None
        ]
        if not reports:
            raise HTTPException(status_code=404, detail="No trading pairs found for this token")

        reports.sort(key=lambda item: item["liquidity"]["usd"], reverse=True)
        return {
            "status": "ok",
            "chain": "solana",
            "source": "DexScreener + Solana RPC",
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "address": address,
            "pairs_found": len(reports),
            "security": security,
            "reports": reports,
            "disclaimer": "Automated screening only. Not financial advice and not a guarantee of safety or profit.",
        }
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Market data provider unavailable")


@app.get("/api/tokens")
async def tokens(
    limit: int = Query(20, ge=1, le=30),
    min_liquidity: float = Query(0, ge=0),
    min_volume: float = Query(0, ge=0),
):
    try:
        async with httpx.AsyncClient(timeout=RPC_TIMEOUT) as client:
            response = await client.get(f"{DEX}/token-profiles/latest/v1")
            response.raise_for_status()
            profiles = response.json()

            chosen = []
            seen = set()
            for item in profiles if isinstance(profiles, list) else []:
                address = item.get("tokenAddress")
                if item.get("chainId") == "solana" and address and address not in seen:
                    chosen.append(item)
                    seen.add(address)
                if len(chosen) >= limit:
                    break

            semaphore = asyncio.Semaphore(6)

            async def load_limited(address):
                async with semaphore:
                    return await get_pairs(client, address)

            results = await asyncio.gather(
                *(load_limited(item["tokenAddress"]) for item in chosen),
                return_exceptions=True,
            )

        output = []
        errors = 0
        for profile, pairs in zip(chosen, results):
            if isinstance(pairs, Exception):
                errors += 1
                continue
            reports = [
                report
                for pair in pairs
                if (report := analyze_pair(pair, profile["tokenAddress"])) is not None
            ]
            if not reports:
                continue
            best = max(reports, key=lambda item: item["liquidity"]["usd"])
            best["profile_url"] = profile.get("url")
            if (
                best["liquidity"]["usd"] >= min_liquidity
                and best["volume"]["24h"] >= min_volume
            ):
                output.append(best)

        output.sort(
            key=lambda item: (item["screening"]["score"], item["volume"]["24h"]),
            reverse=True,
        )
        return {
            "status": "ok",
            "chain": "solana",
            "mode": "paper",
            "tokens": output,
            "profiles_checked": len(chosen),
            "tokens_returned": len(output),
            "pair_lookup_errors": errors,
            "source": "DexScreener",
        }
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="DexScreener request failed")
