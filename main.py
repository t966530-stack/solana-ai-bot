import asyncio
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

DEX = "https://api.dexscreener.com"
app = FastAPI(title="Solana AI Bot API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict to your Ship.page domain before production.
    allow_methods=["GET"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"service": "Solana AI Bot API", "status": "ok", "mode": "paper",
            "health": "/api/health", "tokens": "/api/tokens"}

@app.get("/api/health")
async def health():
    return {"status": "ok", "mode": "paper", "version": "0.2.0"}

def num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0

def score_pair(pair, address):
    base = pair.get("baseToken") or {}
    # priceUsd describes the base token; skip quote-side matches to avoid wrong prices.
    if str(base.get("address", "")).lower() != address.lower():
        return None
    liquidity = num((pair.get("liquidity") or {}).get("usd"))
    volume = num((pair.get("volume") or {}).get("h24"))
    h24 = (pair.get("txns") or {}).get("h24") or {}
    buys, sells = int(num(h24.get("buys"))), int(num(h24.get("sells")))
    txns = buys + sells
    change = num((pair.get("priceChange") or {}).get("h24"))

    # Transparent screening heuristic, not AI or a guarantee of safety/profit.
    score = (30 if liquidity >= 50000 else 20 if liquidity >= 10000
             else 10 if liquidity >= 3000 else 0)
    score += (25 if volume >= 100000 else 15 if volume >= 20000
              else 8 if volume >= 5000 else 0)
    score += 20 if txns >= 500 else 12 if txns >= 100 else 5 if txns >= 25 else 0
    if change > 0:
        score += min(15, int(change / 2))
    signal = "AVOID" if liquidity < 3000 or txns < 10 else "WATCH" if score >= 55 else "REVIEW"

    return {
        "address": address, "symbol": base.get("symbol"), "name": base.get("name"),
        "priceUsd": pair.get("priceUsd"), "liquidityUsd": liquidity,
        "volume24h": volume, "buys24h": buys, "sells24h": sells,
        "transactions24h": txns, "priceChange24h": change,
        "screeningScore": score, "screeningSignal": signal,
        "pairUrl": pair.get("url"), "dexId": pair.get("dexId"),
        "pairCreatedAt": pair.get("pairCreatedAt"),
    }

async def load_pairs(client, sem, address):
    async with sem:
        r = await client.get(f"{DEX}/token-pairs/v1/solana/{address}")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

@app.get("/api/tokens")
async def tokens(
    limit: int = Query(20, ge=1, le=30),
    min_liquidity: float = Query(0, ge=0),
    min_volume: float = Query(0, ge=0),
):
    """Discover latest DexScreener-profiled Solana tokens and screen their best pools."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{DEX}/token-profiles/latest/v1")
            r.raise_for_status()
            profiles = r.json()

            chosen, seen = [], set()
            for item in profiles if isinstance(profiles, list) else []:
                address = item.get("tokenAddress")
                if item.get("chainId") == "solana" and address and address not in seen:
                    chosen.append(item)
                    seen.add(address)
                if len(chosen) >= limit:
                    break

            sem = asyncio.Semaphore(6)
            results = await asyncio.gather(
                *(load_pairs(client, sem, p["tokenAddress"]) for p in chosen),
                return_exceptions=True,
            )

        output, errors = [], 0
        for profile, pairs in zip(chosen, results):
            if isinstance(pairs, Exception):
                errors += 1
                continue
            candidates = [
                item for p in pairs
                if (item := score_pair(p, profile["tokenAddress"])) is not None
            ]
            if not candidates:
                continue
            best = max(candidates, key=lambda x: x["liquidityUsd"])
            best["profileUrl"] = profile.get("url")
            best["description"] = profile.get("description")
            if best["liquidityUsd"] >= min_liquidity and best["volume24h"] >= min_volume:
                output.append(best)

        output.sort(key=lambda x: (x["screeningScore"], x["volume24h"]), reverse=True)
        return {
            "tokens": output, "chain": "solana", "mode": "paper",
            "source": "DexScreener latest token profiles + token pairs",
            "profilesChecked": len(chosen), "tokensReturned": len(output),
            "pairLookupErrors": errors,
            "note": "This feed covers latest profiled tokens, not every Solana mint. "
                    "The score is a simple heuristic, not an AI prediction, safety audit, "
                    "or buy/sell recommendation.",
        }
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"DexScreener request failed: {type(exc).__name__}")
