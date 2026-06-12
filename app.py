"""render-gt-probe — Google Trends probe service.

Tests whether AWS IPs (used by Render's infrastructure) get the same 429
treatment from Google as Hetzner. If GET /probe returns real GT data,
Render is viable for fetching GT signals; if it 429s, AWS is just as
banned as Hetzner and we move on to the next option (DataImpulse
residential / Vultr / SerpAPI).

Endpoints:
  GET /          — service info + outbound IP
  GET /status    — quick alive check (no GT call)
  GET /probe?keyword=cedar+point&timeframe=today+3-m&geo=US
                 — actual pytrends fetch, returns success/429
"""
from __future__ import annotations

import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta

from flask import Flask, jsonify, request

app = Flask(__name__)


def _outbound_ip() -> str:
    """Best-effort outbound IP detection. Helpful for diagnosing whether
    we hit Render's expected IP range."""
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=10) as resp:
            return resp.read().decode().strip()
    except Exception as e:
        return f"unknown({type(e).__name__})"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@app.get("/")
def root():
    return jsonify({
        "service": "render-gt-probe",
        "purpose": "Test whether Render's AWS-backed IPs avoid the "
                   "Google Trends /64 ban that Hetzner triggers.",
        "endpoints": ["/status", "/probe?keyword=cedar+point"],
        "outbound_ip": _outbound_ip(),
        "ts": _now_iso(),
    })


@app.get("/status")
def status():
    return jsonify({
        "ok": True,
        "outbound_ip": _outbound_ip(),
        "ts": _now_iso(),
    })


@app.get("/fred_gas")
def fred_gas():
    """Fetch FRED weekly US gas price series (GASREGW) and return as
    {date_str: float_usd_per_gallon}. VPS proxies through here because
    Hetzner's network can't reach fred.stlouisfed.org reliably.

    Returns: {ok, n_weeks, latest_date, latest_value, data: {date: value}}
    """
    import csv as _csv
    import io as _io
    import urllib.request as _ur
    started = time.time()
    ip = _outbound_ip()
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=GASREGW"
    try:
        req = _ur.Request(url, headers={"User-Agent": "render-gt-probe/1.0"})
        with _ur.urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
        reader = _csv.DictReader(_io.StringIO(text))
        result: dict[str, float] = {}
        for row in reader:
            v = row.get("GASREGW", ".")
            if v and v != ".":
                try:
                    result[row["DATE"]] = float(v)
                except ValueError:
                    pass
        if not result:
            return jsonify({
                "ok": False,
                "outbound_ip": ip,
                "elapsed_s": round(time.time() - started, 2),
                "error": "FRED returned empty/unparseable data",
            }), 502
        latest_date = max(result.keys())
        return jsonify({
            "ok": True,
            "outbound_ip": ip,
            "n_weeks": len(result),
            "latest_date": latest_date,
            "latest_value": result[latest_date],
            "elapsed_s": round(time.time() - started, 2),
            "data": result,
        })
    except Exception as e:
        msg = str(e)[:300]
        return jsonify({
            "ok": False,
            "outbound_ip": ip,
            "elapsed_s": round(time.time() - started, 2),
            "error": f"{type(e).__name__}: {msg}",
        }), 502


@app.get("/fetch_park")
def fetch_park():
    """Production fetcher: given park_name + optional dates list, run the
    full per-park multi-keyword GT fetch + z-score computation and return
    the dict that fetch_google_trends_for_park would produce locally.

    Query params:
      park (str)   — required; must match PARK_TREND_QUERIES key
      dates (str)  — comma-separated YYYY-MM-DD list. Default: last 90 days.

    Returns: {date_str: {gt_intent_z, gt_logistics_z, gt_hotel_z,
                          gt_composite_z, gt_intent_slope, gt_logistics_spike,
                          gt_hotel_slope}}
    """
    park = request.args.get("park")
    if not park:
        return jsonify({"ok": False, "error": "missing ?park= query param"}), 400

    dates_raw = request.args.get("dates", "")
    if dates_raw:
        dates = [d.strip() for d in dates_raw.split(",") if d.strip()]
    else:
        # Default: last 90 days
        today = datetime.now(timezone.utc).date()
        dates = [(today - timedelta(days=i)).isoformat()
                 for i in range(90, -1, -1)]

    started = time.time()
    ip = _outbound_ip()
    try:
        from google_trends import fetch_google_trends_for_park
        result = fetch_google_trends_for_park(
            park, dates, force_refresh=True, cache_only=False,
        )
        if not result:
            return jsonify({
                "ok": False,
                "park": park,
                "outbound_ip": ip,
                "n_dates": len(dates),
                "elapsed_s": round(time.time() - started, 2),
                "error": "empty result (park not in PARK_TREND_QUERIES, "
                         "or all keywords 429ed)",
            }), 502
        nonzero = sum(
            1 for v in result.values()
            if isinstance(v, dict) and abs(v.get("gt_intent_z", 0)) > 0.001
        )
        return jsonify({
            "ok": True,
            "park": park,
            "outbound_ip": ip,
            "n_dates": len(dates),
            "n_dates_with_data": nonzero,
            "elapsed_s": round(time.time() - started, 2),
            "data": result,
        })
    except Exception as e:
        msg = str(e)[:300]
        code = 429 if "429" in msg or "Too Many" in msg else 502
        return jsonify({
            "ok": False,
            "park": park,
            "outbound_ip": ip,
            "elapsed_s": round(time.time() - started, 2),
            "error": f"{type(e).__name__}: {msg}",
        }), code


@app.get("/probe")
def probe():
    """Run one pytrends query. Returns the result or the failure mode.

    Query params:
      keyword (str)   — default "cedar point"
      timeframe (str) — default "today 3-m" (90 days)
      geo (str)       — default "US"
    """
    keyword = request.args.get("keyword", "cedar point")
    timeframe = request.args.get("timeframe", "today 3-m")
    geo = request.args.get("geo", "US")

    started = time.time()
    ip = _outbound_ip()

    result = {
        "keyword": keyword,
        "timeframe": timeframe,
        "geo": geo,
        "outbound_ip": ip,
        "started_at": _now_iso(),
    }

    # Import pytrends lazily so /status + / work even if the import
    # blows up (e.g. urllib3 version mismatch — pytrends is fragile).
    try:
        from pytrends.request import TrendReq
    except Exception as e:
        result.update({
            "ok": False,
            "error": f"pytrends import failed: {type(e).__name__}: {e}",
            "elapsed_s": round(time.time() - started, 2),
        })
        return jsonify(result), 500

    try:
        py = TrendReq(timeout=(15, 30), retries=1, backoff_factor=2)
        py.build_payload([keyword], timeframe=timeframe, geo=geo)
        df = py.interest_over_time()
        if df is None or df.empty:
            result.update({
                "ok": False,
                "error": "empty result (typically 429 with retry exhausted, "
                         "OR pytrends rejected the response)",
                "elapsed_s": round(time.time() - started, 2),
            })
            return jsonify(result), 502
        latest = df.tail(5).reset_index().to_dict(orient="records")
        # Ensure JSON-serializable (Timestamps → ISO)
        for row in latest:
            for k, v in list(row.items()):
                if hasattr(v, "isoformat"):
                    row[k] = v.isoformat()
        result.update({
            "ok": True,
            "n_rows": len(df),
            "latest_5_periods": latest,
            "elapsed_s": round(time.time() - started, 2),
        })
        return jsonify(result)
    except Exception as e:
        msg = str(e)[:300]
        code = 429 if "429" in msg or "Too Many" in msg else 502
        result.update({
            "ok": False,
            "error": f"{type(e).__name__}: {msg}",
            "elapsed_s": round(time.time() - started, 2),
        })
        return jsonify(result), code


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
