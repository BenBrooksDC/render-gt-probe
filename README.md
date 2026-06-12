# render-gt-probe

Tiny Flask service to test whether [Render](https://render.com/)'s AWS-backed
infrastructure can fetch Google Trends data, or whether Google's `/64`-level
rate-limiter that hits Hetzner ASN AS24940 also hits Render's AWS ASN AS16509.

Background: a Hetzner VPS was getting `429` on every `pytrends` call. We
verified Google bans the entire IPv6 `/64` rather than individual addresses.
Different ASN should mean a different rate-limit bucket — this probe verifies.

## What's in here

- `app.py` — Flask app with three endpoints (`/`, `/status`, `/probe`)
- `requirements.txt` — Flask + pytrends + gunicorn
- `render.yaml` — Render blueprint, free tier, Python runtime
- `.gitignore` — standard Python excludes

## Deploy to Render (free tier)

1. Sign in to https://render.com (GitHub auth)
2. **New +** → **Blueprint** → paste this repo's URL
3. Render reads `render.yaml`, provisions the free web service
4. Wait ~3-5 min for build + first deploy
5. Service URL will look like `https://render-gt-probe.onrender.com`

## Test it

```bash
# Quick alive check — should return JSON with outbound_ip
curl https://render-gt-probe.onrender.com/status

# The real test — fetches Google Trends for "cedar point" over 90d
curl 'https://render-gt-probe.onrender.com/probe?keyword=cedar+point'
```

**Successful** result looks like:
```json
{
  "ok": true,
  "outbound_ip": "44.230.x.x",
  "n_rows": 12,
  "latest_5_periods": [...],
  "elapsed_s": 4.21
}
```

**Banned** result (Google rate-limited Render's IP too) looks like:
```json
{
  "ok": false,
  "error": "TooManyRequestsError: The request failed: Google returned a response with code 429",
  "outbound_ip": "44.230.x.x",
  "elapsed_s": 0.91
}
```
HTTP status will be `429`.

## Custom queries

The `/probe` endpoint accepts query params:

- `keyword` — what to search (default `cedar point`)
- `timeframe` — pytrends window string (default `today 3-m`; see [pytrends docs](https://github.com/GeneralMills/pytrends#interest-over-time))
- `geo` — ISO country code (default `US`)

Example:
```bash
curl 'https://render-gt-probe.onrender.com/probe?keyword=disneyland&timeframe=now+7-d&geo=US-CA'
```

## What we're learning

| Outcome | Verdict |
|---|---|
| First `/probe` returns `ok: true` | Render's AWS IPs are clean for GT. Move fetch_gt to a $1/mo Render cron job. |
| First `/probe` returns `429` | AWS is just as banned as Hetzner. Move on to DataImpulse residential ($2-5/mo) or SerpAPI ($50/mo). |
| First works, but hammering 5 in a row triggers 429 | Per-IP rate-limit is working — design fetch_gt around a 30+ second interval, no proxy needed. |

## After the test

The Render free tier spins the service down after 15 min idle and gives
~30s cold start. That's fine for a probe but bad for a scheduled fetcher.
If verdict is "works": upgrade to a **$1/mo Render cron job** running
`python -m fetch_gt` on a schedule and posting results to the Thoosie VPS.

## License

This is a throwaway test probe. MIT.
