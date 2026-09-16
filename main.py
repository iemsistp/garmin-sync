#!/usr/bin/env python3
"""
Garmin → Supabase sync server via Garth
Deploy on Railway. Exposes POST /sync and runs a daily cron at 7am.
"""

import os
import json
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import garth
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]
GARMIN_EMAIL    = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD = os.environ["GARMIN_PASSWORD"]
GARTH_CACHE     = "/tmp/garth_cache"

# ── AUTH ──────────────────────────────────────────────────
def get_garth():
    if Path(GARTH_CACHE).exists():
        try:
            garth.resume(GARTH_CACHE)
            garth.client.username
            return
        except Exception:
            pass
    garth.login(GARMIN_EMAIL, GARMIN_PASSWORD)
    garth.save(GARTH_CACHE)

# ── FETCH ONE DATE ────────────────────────────────────────
def fetch_date(target: str) -> dict:
    row = {"date": target}

    # Sleep
    try:
        sleep = garth.connectapi(f"/wellness-service/wellness/dailySleepData/{target}")
        dto = sleep.get("dailySleepDTO", {})
        row["sleep_score"]         = (dto.get("sleepScores") or {}).get("overall", {}).get("value")
        row["sleep_duration_hrs"]  = round(dto["sleepTimeSeconds"] / 3600, 2) if dto.get("sleepTimeSeconds") else None
        row["sleep_deep_min"]      = round(dto["deepSleepSeconds"] / 60) if dto.get("deepSleepSeconds") else None
        row["sleep_light_min"]     = round(dto["lightSleepSeconds"] / 60) if dto.get("lightSleepSeconds") else None
        row["sleep_rem_min"]       = round(dto["remSleepSeconds"] / 60) if dto.get("remSleepSeconds") else None
        row["sleep_awake_min"]     = round(dto["awakeSleepSeconds"] / 60) if dto.get("awakeSleepSeconds") else None
    except Exception as e:
        print(f"  sleep error: {e}")

    # Daily summary
    try:
        summary = garth.connectapi(f"/usersummary-service/usersummary/daily/{target}")
        row["calories_burned"] = summary.get("totalKilocalories")
        row["resting_hr"]      = summary.get("restingHeartRate")
        row["stress_avg"]      = summary.get("averageStressLevel")
    except Exception as e:
        print(f"  summary error: {e}")

    # Body battery
    try:
        bb = garth.connectapi(f"/wellness-service/wellness/dailyBodyBattery/{target}")
        if isinstance(bb, list) and bb:
            row["body_battery_high"] = max((b.get("charged", 0) for b in bb), default=None)
    except Exception as e:
        print(f"  body battery error: {e}")

    # Training readiness
    try:
        tr = garth.connectapi(f"/metrics-service/metrics/trainingReadiness/{target}")
        dto2 = tr.get("trainingReadinessDTO", {})
        row["training_readiness"] = dto2.get("score")
        row["training_status"]    = dto2.get("trainingStatusPhrase")
    except Exception as e:
        print(f"  training readiness error: {e}")

    # HRV
    try:
        hrv = garth.connectapi(f"/hrv-service/hrv/{target}")
        hrv_summary = hrv.get("hrvSummary", {})
        row["hrv_7day_avg"]  = hrv_summary.get("weekly")
        row["overnight_hrv"] = hrv_summary.get("lastNight")
    except Exception as e:
        print(f"  hrv error: {e}")

    return row

# ── PUSH TO SUPABASE ──────────────────────────────────────
def push_to_supabase(row: dict):
    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/garmin_daily",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        params={"on_conflict": "date"},
        json=row,
    )
    if res.ok:
        print(f"  ✓ pushed {row['date']}")
    else:
        print(f"  ✗ supabase error {res.status_code}: {res.text}")
    return res.ok

# ── SYNC LOGIC ────────────────────────────────────────────
def run_sync(target_date: str = None):
    get_garth()
    dates = [
        (date.today() - timedelta(days=1)).isoformat(),
        date.today().isoformat(),
    ] if not target_date else [target_date]

    results = []
    for d in dates:
        print(f"📥 Fetching {d}...")
        row = fetch_date(d)
        ok = push_to_supabase(row)
        results.append({"date": d, "ok": ok, "data": row})
    return results

# ── ROUTES ────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "garmin-sync running"}

@app.post("/sync")
def sync(date: str = None):
    try:
        results = run_sync(date)
        return {"success": True, "results": results}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── DAILY CRON (7am UTC) ──────────────────────────────────
def cron_loop():
    while True:
        now = __import__("datetime").datetime.utcnow()
        # Sleep until next 7:00 UTC
        next_run = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run = next_run + timedelta(days=1)
        sleep_secs = (next_run - now).total_seconds()
        print(f"⏰ Next auto-sync in {sleep_secs/3600:.1f}h")
        time.sleep(sleep_secs)
        print("⏰ Running scheduled sync...")
        try:
            run_sync()
        except Exception as e:
            print(f"  cron error: {e}")

# Start cron thread on boot
threading.Thread(target=cron_loop, daemon=True).start()
