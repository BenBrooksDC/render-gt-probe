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
from datetime import datetime, timezone

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
