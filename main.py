from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx

app = FastAPI(title="Solana AI Bot API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/api/health")
async def health():
    return {"status": "ok", "mode": "paper"}

@app.get("/api/tokens")
async def tokens():
    # Public market discovery only. No wallet keys or transactions are used.
    url = "https://api.dexscreener.com/latest/dex/search?q=SOL"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()
    pairs = [p for p in data.get("pairs", []) if p.get("chainId") == "solana"]
    pairs.sort(key=lambda p: (p.get("volume", {}).get("h24") or 0), reverse=True)
    out = []
    for p in pairs[:30]:
        base = p.get("baseToken") or {}
        out.append({
            "symbol": base.get("symbol"),
            "name": base.get("name"),
            "priceUsd": p.get("priceUsd") or 0,
            "liquidityUsd": (p.get("liquidity") or {}).get("usd") or 0,
            "volume24h": (p.get("volume") or {}).get("h24") or 0,
            "url": p.get("url")
        })
    return {"tokens": out, "source": "DexScreener", "note": "Discovery is not a safety verdict."}
