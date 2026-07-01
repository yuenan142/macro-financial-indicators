import os, json, time, requests
from pathlib import Path

# Where JSON outputs are written (served by GitHub Pages)
DATA = Path("data"); DATA.mkdir(exist_ok=True)

# Get your FRED key from env (set in GitHub Secrets or locally via export)
FRED_KEY = os.environ["FRED_API_KEY"]

# ── proxy helpers ──────────────────────────────────────────────────
PROXIES = None
if os.environ.get("https_proxy") or os.environ.get("http_proxy"):
    PROXIES = {
        "https": os.environ.get("https_proxy"),
        "http":  os.environ.get("http_proxy"),
    }

def write_json(name, obj):
    obj["_meta"] = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2))

def fred_series(series_id: str) -> dict:
    """Fetch series from FRED API with proxy, rate-limit retry, and 1.5s inter-request delay."""
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {"series_id": series_id, "api_key": FRED_KEY, "file_type": "json"}

    r = requests.get(url, params=params, timeout=30, proxies=PROXIES)

    # Only act on rate limit - keeps normal runs fast
    if r.status_code == 429:
        print(f"Rate limit hit for {series_id} - retrying after short wait...")
        time.sleep(10)                    # only triggers on 429
        r = requests.get(url, params=params, timeout=30, proxies=PROXIES)

    r.raise_for_status()

    obs = r.json().get("observations", [])
    points = [{"date": o["date"], "value": (None if o["value"] == "." else float(o["value"]))} for o in obs]
    # Add 1.5s delay between FRED requests to avoid throttling
    time.sleep(1.5)
    return {"series_id": series_id, "points": points, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# ═══════════════════════════════════════════════════════════════════
#  ORIGINAL INDICATORS
# ═══════════════════════════════════════════════════════════════════

def fetch_original_indicators():
    """Fetch the 7 original macro indicators."""
    print("── Original indicators ──")
    write_json("m2.json", fred_series("M2SL"))
    print("  ✓ M2")
    write_json("usd_index.json", fred_series("DTWEXBGS"))
    print("  ✓ USD Index")
    write_json("yield_2y.json", fred_series("DGS2"))
    print("  ✓ 2Y Yield")
    write_json("yield_10y.json", fred_series("DGS10"))
    print("  ✓ 10Y Yield")
    write_json("spread_10y_2y.json", fred_series("T10Y2Y"))
    print("  ✓ Spread")
    write_json("vix.json", fred_series("VIXCLS"))
    print("  ✓ VIX")
    write_json("cpi.json", fred_series("CPIAUCSL"))
    print("  ✓ CPI")
    write_json("fedfunds.json", fred_series("FEDFUNDS"))
    print("  ✓ Fed Funds")
    write_json("usdjpy.json", fred_series("DEXJPUS"))
    print("  ✓ USDJPY")
    write_json("tips.json", fred_series("DFII10"))
    print("  ✓ TIPS (10Y Real Rate)")
    write_json("boj_rate.json", fred_series("IR3TIB01JPM156N"))
    print("  ✓ BOJ Rate")


# ═══════════════════════════════════════════════════════════════════
#  NEW INDICATOR 1: Fed Net Liquidity  (WALCL - RRP - TGA)
# ═══════════════════════════════════════════════════════════════════

def compute_fed_net_liquidity():
    """
    Fed Net Liquidity = WALCL - (RRPONTSYD × 1000) - WTREGEN
    WALCL:  millions USD (Fed total assets)
    RRPONTSYD: billions USD (overnight reverse repo) → ×1000 to millions
    WTREGEN: millions USD (Treasury General Account)
    """
    print("\n── Fed Net Liquidity ──")
    walcl      = fred_series("WALCL")
    print("  ✓ WALCL (Fed Assets)")
    rrpointsyd = fred_series("RRPONTSYD")
    print("  ✓ RRPONTSYD (O/N Reverse Repo)")
    wtregn     = fred_series("WTREGEN")
    print("  ✓ WTREGEN (TGA)")

    # Build lookup dicts keyed by date
    rrp_lookup = {p["date"]: p["value"] for p in rrpointsyd["points"] if p["value"] is not None}
    tga_lookup = {p["date"]: p["value"] for p in wtregn["points"]     if p["value"] is not None}

    net_liq_points = []
    for p in walcl["points"]:
        if p["value"] is None:
            continue
        date = p["date"]
        walcl_val = p["value"]                    # millions
        rrp_val   = rrp_lookup.get(date)          # billions
        tga_val   = tga_lookup.get(date)           # millions

        if rrp_val is None or tga_val is None:
            # Skip dates where we lack RRP or TGA data
            continue

        net = walcl_val - (rrp_val * 1000.0) - tga_val   # all in millions
        net_liq_points.append({"date": date, "value": round(net, 2)})

    result = {
        "series_id": "NET_LIQ",
        "description": "Fed Net Liquidity = WALCL - RRP - TGA (millions USD)",
        "components": {"WALCL": "millions", "RRPONTSYD": "billions (×1000)", "WTREGEN": "millions"},
        "points": net_liq_points,
    }
    write_json("fed_net_liq.json", result)
    print(f"  → Fed Net Liquidity: {len(net_liq_points)} data points saved")


# ═══════════════════════════════════════════════════════════════════
#  NEW INDICATOR 2: Global Central Bank Balance Sheet
# ═══════════════════════════════════════════════════════════════════

def compute_global_cb_balance():
    """
    Global CB Balance Sheet = Fed(WALCL millions USD) +
                              ECB(ECBASSETSW millions EUR → USD) +
                              BOJ(JPNASSETS 100M yen → USD)

    Uses FRED exchange rates:
      - DEXUSEU: USD per 1 EUR
      - DEXJPUS: JPY per 1 USD  (so USD = JPNASSETS × 100M / DEXJPUS / 1M = JPNASSETS × 100 / DEXJPUS  millions USD)
    """
    print("\n── Global Central Bank Balance Sheet ──")
    walcl      = fred_series("WALCL")
    print("  ✓ WALCL (Fed, millions USD)")
    ecb        = fred_series("ECBASSETSW")
    print("  ✓ ECBASSETSW (ECB, millions EUR)")
    boj        = fred_series("JPNASSETS")
    print("  ✓ JPNASSETS (BOJ, 100M JPY)")
    fx_eur     = fred_series("DEXUSEU")
    print("  ✓ DEXUSEU (USD per EUR)")
    fx_jpy     = fred_series("DEXJPUS")
    print("  ✓ DEXJPUS (JPY per USD)")

    # Build lookups
    walcl_lookup  = {p["date"]: p["value"] for p in walcl["points"]  if p["value"] is not None}
    ecb_lookup    = {p["date"]: p["value"] for p in ecb["points"]    if p["value"] is not None}
    boj_lookup    = {p["date"]: p["value"] for p in boj["points"]    if p["value"] is not None}
    fx_eur_lookup = {p["date"]: p["value"] for p in fx_eur["points"] if p["value"] is not None}
    fx_jpy_lookup = {p["date"]: p["value"] for p in fx_jpy["points"] if p["value"] is not None}

    # Use Walcl dates as the timeline spine
    cb_points = []
    for date in sorted(walcl_lookup.keys()):
        f_val = walcl_lookup.get(date)
        e_val = ecb_lookup.get(date)
        j_val = boj_lookup.get(date)
        eur   = fx_eur_lookup.get(date)
        jpy   = fx_jpy_lookup.get(date)

        if f_val is None:
            continue

        total = f_val  # Fed already in millions USD

        # ECB: EUR millions → USD millions
        if e_val is not None and eur is not None:
            total += e_val * eur

        # BOJ: 100M yen → USD millions
        # JPNASSETS is in 100M yen units; DEXJPUS is JPY per USD
        # USD millions = (JPNASSETS × 100,000,000) / DEXJPUS / 1,000,000
        #              = JPNASSETS × 100 / DEXJPUS
        if j_val is not None and jpy is not None:
            total += j_val * 100.0 / jpy

        cb_points.append({"date": date, "value": round(total, 2)})

    result = {
        "series_id": "GLOBAL_CB",
        "description": "Global Central Bank Balance Sheet (Fed + ECB + BOJ) in millions USD",
        "components": {
            "WALCL": "Fed (millions USD)",
            "ECBASSETSW": "ECB (millions EUR → USD via DEXUSEU)",
            "JPNASSETS": "BOJ (100M JPY → USD via DEXJPUS)",
        },
        "points": cb_points,
    }
    write_json("global_cb.json", result)
    print(f"  → Global CB Balance Sheet: {len(cb_points)} data points saved")


# ═══════════════════════════════════════════════════════════════════
#  NEW INDICATOR 3: Stablecoin Market Cap (CoinGecko)
# ═══════════════════════════════════════════════════════════════════

def fetch_stablecoin_mcap():
    """
    Fetch stablecoin total market cap from CoinGecko API.
    Uses /api/v3/global endpoint, field: data.total_market_cap.stablecoins (deprecated but still works)
    Fallback: try /api/v3/simple/price with ids=... or just return the raw global data.
    """
    print("\n── Stablecoin Market Cap (CoinGecko) ──")
    url = "https://api.coingecko.com/api/v3/global"
    try:
        r = requests.get(url, timeout=30, proxies=PROXIES)
        r.raise_for_status()
        data = r.json()
        # Try stablecoins field first
        stablecoin_mcap = None
        if "data" in data:
            if "total_market_cap" in data["data"]:
                stablecoin_mcap = data["data"]["total_market_cap"].get("stablecoins")

        # Fallback: try getting top stablecoins market cap via search
        if stablecoin_mcap is None:
            print("  ⚠ stablecoins field not found, trying top-stablecoin-by-market-cap query...")
            # Fetch top stablecoins: USDT, USDC, DAI
            sc_url = "https://api.coingecko.com/api/v3/simple/price"
            sc_params = {
                "ids": "tether,usd-coin,dai",
                "vs_currencies": "usd",
                "include_market_cap": "true",
            }
            r2 = requests.get(sc_url, params=sc_params, timeout=30, proxies=PROXIES)
            r2.raise_for_status()
            sc_data = r2.json()
            total = 0
            for coin_id, info in sc_data.items():
                if "usd_market_cap" in info:
                    total += info["usd_market_cap"]
            stablecoin_mcap = round(total, 2) if total > 0 else None

        # Build a simple JSON with the single data point
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        result = {
            "series_id": "STABLECOIN_MCAP",
            "description": "Stablecoin total market cap (USD) from CoinGecko",
            "source": "CoinGecko API /api/v3/global",
            "points": [{"date": time.strftime("%Y-%m-%d"), "value": stablecoin_mcap}],
            "updated_at": timestamp,
        }
        write_json("stablecoin_mcap.json", result)
        val_str = f"${stablecoin_mcap:,.0f}" if stablecoin_mcap else "NULL"
        print(f"  ✓ Stablecoin MCap: {val_str}")
        return result
    except Exception as e:
        print(f"  ✗ Stablecoin fetch failed: {e}")
        # Write a placeholder so the frontend doesn't break
        result = {
            "series_id": "STABLECOIN_MCAP",
            "description": "Stablecoin total market cap (USD) - fetch failed",
            "source": "CoinGecko API",
            "points": [],
            "error": str(e),
        }
        write_json("stablecoin_mcap.json", result)
        return result


# ═══════════════════════════════════════════════════════════════════
#  NEW INDICATOR 4: Arthur Hayes RRP Pipeline Signal
# ═══════════════════════════════════════════════════════════════════

def compute_rrp_signal():
    """
    Arthur Hayes liquidity pipeline signal based on RRP exhaustion rate.

    Phases:
    - 🟢 Fast drain + lots left → hold (bullish)
    - 🟡 Fast drain + nearly empty → prepare exit (cautious)
    - 🟠 Slowing + near zero → alert (bearish signal approaching)
    - 🔴 RRP rising → tightening, reduce positions (bearish)

    Uses RRPONTSYD data. Computes:
      - Current RRP level
      - 4-week change rate (annualized)
      - Peak RRP
      - Remaining % of peak
    """
    print("\n── Arthur Hayes RRP Pipeline Signal ──")
    rrpointsyd = fred_series("RRPONTSYD")
    print("  ✓ RRPONTSYD (for signal)")

    # Filter to points with values, sorted by date
    points = [p for p in rrpointsyd["points"] if p["value"] is not None]
    points.sort(key=lambda p: p["date"])

    if len(points) < 30:
        print("  ⚠ Not enough data points for signal")
        return

    current  = points[-1]
    peak     = max(points, key=lambda p: p["value"])
    month_ago_idx = max(0, len(points) - 22)  # ~22 trading days ≈ 1 month
    month_ago = points[month_ago_idx]

    current_val = current["value"]     # billions
    peak_val    = peak["value"]
    remaining_pct = (current_val / peak_val * 100) if peak_val > 0 else 0

    # 4-week change (negative = draining, positive = rising)
    change_4w = current_val - month_ago["value"]
    # Daily drain rate over last 4 weeks (22 trading days)
    daily_drain = change_4w / max(1, (len(points) - 1 - month_ago_idx))

    # Determine phase
    phase = ""
    phase_color = ""
    signal_text = ""
    signal_detail = ""

    if change_4w < -0.5:  # Fast drain (more than 0.5B/week decline)
        if remaining_pct > 15:
            phase = "🟢 放水阶段 / Liquidity Easing"
            phase_color = "#34d399"
            signal_text = "继续持有 / Hold — RRP快速消耗，流动性充裕"
            signal_detail = f"RRP仍在快速下降中（4周变化 {change_4w:+.1f}B），剩余 {remaining_pct:.1f}% 的峰值缓冲，美元流动性继续释放，利好风险资产。"
        else:
            phase = "🟡 接近枯竭 / Near Exhaustion"
            phase_color = "#f59e0b"
            signal_text = "准备退出 / Prepare Exit — RRP即将耗尽"
            signal_detail = f"RRP已从峰值${peak_val:,.0f}B 降至 ${current_val:,.1f}B（仅剩 {remaining_pct:.1f}%），消耗速度仍然较快（4周 {change_4w:+.1f}B），流动性释放接近尾声，需要密切关注。"
    elif change_4w > 0.5:  # Rising
        phase = "🔴 紧缩 / Tightening"
        phase_color = "#f87171"
        signal_text = "减仓 / Reduce — RRP回升，流动性收紧"
        signal_detail = f"RRP近4周上升 {change_4w:+.1f}B，表明市场流动性正在被抽走（资金从风险资产回流至逆回购），这是明确的紧缩信号。当前RRP: ${current_val:,.1f}B。"
    else:
        # Slow / near-zero change
        if remaining_pct < 10:
            phase = "🟠 停滞 / Stalling Near Zero"
            phase_color = "#f97316"
            signal_text = "警惕 / Alert — RRP基本耗尽，流动性拐点临近"
            signal_detail = f"RRP已降至 ${current_val:,.1f}B（仅剩峰值 {remaining_pct:.1f}%），且消耗速度放缓（4周 {change_4w:+.1f}B）。Arthur Hayes理论认为这意味着流动性释放即将结束，市场可能面临回调压力。"

        else:
            phase = "🟢 稳定 / Steady"
            phase_color = "#34d399"
            signal_text = "继续持有 / Hold — RRP稳定，流动性中性"
            signal_detail = f"RRP变化不大（4周 {change_4w:+.1f}B），当前 ${current_val:,.1f}B，剩余峰值 {remaining_pct:.1f}%。流动性环境相对中性。"

    result = {
        "series_id":     "RRP_SIGNAL",
        "description":   "Arthur Hayes RRP Pipeline Signal — based on Fed O/N Reverse Repo exhaustion rate",
        "source":        "FRED RRPONTSYD + Hayes liquidity framework",
        "current_rrp_b": current_val,
        "peak_rrp_b":    peak_val,
        "peak_date":     peak["date"],
        "remaining_pct": round(remaining_pct, 2),
        "change_4w_b":   round(change_4w, 2),
        "daily_drain_b": round(daily_drain, 4),
        "phase":         phase,
        "phase_color":   phase_color,
        "signal":        signal_text,
        "detail":        signal_detail,
        "points":        points[-90:],  # last ~90 trading days for chart
    }
    write_json("rrp_signal.json", result)
    print(f"  → Phase: {phase}")
    print(f"  → Signal: {signal_text}")
    print(f"  → RRP: ${current_val:,.1f}B  |  Peak: ${peak_val:,.0f}B  |  Remaining: {remaining_pct:.1f}%  |  4W Δ: {change_4w:+.1f}B")




# ═══════════════════════════════════════════════════════════════════
#  NEW INDICATOR 5: BTC Fear & Greed Index
# ═══════════════════════════════════════════════════════════════════

def fetch_fear_greed():
    print("\\n── Fear & Greed Index ──")
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=90", timeout=15)
        r.raise_for_status()
        data = r.json()["data"]
        points = []
        for entry in data:
            ts = int(entry["timestamp"])
            from datetime import datetime
            dt = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            points.append({"date": dt, "value": int(entry["value"]), "label": entry["value_classification"]})
        points.reverse()
        result = {"series_id": "FEAR_GREED", "description": "BTC Fear & Greed Index (0-100)", "source": "alternative.me", "points": points}
        write_json("fear_greed.json", result)
        print(f"  ✓ Fear & Greed: {points[-1]['value']} ({points[-1]['label']})")
    except Exception as e:
        print(f"  ✗ Fear & Greed failed: {e}")
        write_json("fear_greed.json", {"series_id": "FEAR_GREED", "points": [], "error": str(e)})

# ═══════════════════════════════════════════════════════════════════
#  NEW INDICATOR 6: Bitcoin Halving Cycle
# ═══════════════════════════════════════════════════════════════════

def compute_halving():
    print("\\n── Halving Cycle ──")
    from datetime import datetime
    last = datetime(2024, 4, 20)
    nxt  = datetime(2028, 3, 1)
    now  = datetime.utcnow()
    days = (now - last).days
    total = (nxt - last).days
    pct = round(days / total * 100, 1)
    result = {"series_id": "HALVING", "last_halving": "2024-04-20", "next_halving_est": "2028-03-01", "days_since": days, "progress_pct": pct, "months_since": round(days / 30.44, 1)}
    write_json("halving.json", result)
    print(f"  ✓ {days} days / {total} ({pct}%)")

# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    t0 = time.time()

    # Original 8 indicators
    fetch_original_indicators()

    # New indicators
    compute_fed_net_liquidity()
    compute_global_cb_balance()
    fetch_stablecoin_mcap()
    compute_rrp_signal()
    fetch_fear_greed()
    compute_halving()

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"All data fetches complete in {elapsed:.1f}s")
    print(f"{'='*60}")
    print("OK")
