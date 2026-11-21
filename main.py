
import asyncio
import re
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

DEX = "https://api.dexscreener.com"
APP_VERSION = "0.3.0"

app = FastAPI(
    title="Solana AI Bot API",
    version=APP_VERSION
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

def num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0

def valid_address(address):
    return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address))

def analyze_pair(pair, address):
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

    score += 30 if liquidity >= 50000 else (
        20 if liquidity >= 10000 else
        10 if liquidity >= 3000 else 0
    )

    score += 25 if num(volume.get("h24")) >= 100000 else (
        15 if num(volume.get("h24")) >= 20000 else
        8 if num(volume.get("h24")) >= 5000 else 0
    )

    score += 20 if total >= 500 else (
        12 if total >= 100 else
        5 if total >= 25 else 0
    )

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
            (datetime.now(timezone.utc).timestamp() * 1000
             - num(created)) / 3600000
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
        "liquidity": {
            "usd": liquidity
        },
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
            "signal": (
                "AVOID" if liquidity < 3000 or total < 10
                else "WATCH" if score >= 55
                else "REVIEW"
            ),
        },
        "limitations": [
            "This is market-data screening, not a full security audit.",
            "Mint and freeze authorities are not checked.",
            "Holder concentration and wallet links are not checked.",
            "A low risk score does not guarantee safety or profit."
        ],
    }

async def get_pairs(client, address):
    url = f"{DEX}/token-pairs/v1/solana/{address}"

    response = await client.get(url)
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
    }

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "mode": "paper",
        "version": APP_VERSION,
    }

@app.get("/api/analyze/{address}")
async def analyze_token(address: str):
    if not valid_address(address):
        raise HTTPException(
            status_code=400,
            detail="Invalid Solana token address"
        )

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            pairs = await get_pairs(client, address)

        reports = [
            report
            for pair in pairs
            if (report := analyze_pair(pair, address)) is not None
        ]

        if not reports:
            raise HTTPException(
                status_code=404,
                detail="No trading pairs found for this token"
            )

        reports.sort(
            key=lambda item: item["liquidity"]["usd"],
            reverse=True
        )

        return {
            "status": "ok",
            "chain": "solana",
            "source": "DexScreener",
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "address": address,
            "pairs_found": len(reports),
            "reports": reports,
        }

    except httpx.HTTPError:
        raise HTTPException(
            status_code=502,
            detail="Market data provider unavailable"
        )

@app.get("/api/tokens")
async def tokens(
    limit: int = Query(20, ge=1, le=30),
    min_liquidity: float = Query(0, ge=0),
    min_volume: float = Query(0, ge=0),
):
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{DEX}/token-profiles/latest/v1"
            )
            response.raise_for_status()
            profiles = response.json()

            chosen = []
            seen = set()

            for item in profiles if isinstance(profiles, list) else []:
                address = item.get("tokenAddress")

                if (
                    item.get("chainId") == "solana"
                    and address
                    and address not in seen
                ):
                    chosen.append(item)
                    seen.add(address)

                if len(chosen) >= limit:
                    break

            sem = asyncio.Semaphore(6)

            async def load_limited(address):
                async with sem:
                    return await get_pairs(client, address)

            results = await asyncio.gather(
                *[
                    load_limited(item["tokenAddress"])
                    for item in chosen
                ],
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
                if (report := analyze_pair(
                    pair, profile["tokenAddress"]
                )) is not None
            ]

            if not reports:
                continue

            best = max(
                reports,
                key=lambda item: item["liquidity"]["usd"]
            )

            best["profile_url"] = profile.get("url")

            if (
                best["liquidity"]["usd"] >= min_liquidity
                and best["volume"]["24h"] >= min_volume
            ):
                output.append(best)

        output.sort(
            key=lambda item: (
                item["screening"]["score"],
                item["volume"]["24h"]
            ),
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
        raise HTTPException(
            status_code=502,
            detail="DexScreener request failed"
        )
