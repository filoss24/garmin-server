import os
import datetime
import logging
from flask import Flask, jsonify, request, abort
from flask_cors import CORS
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("garmin-server")

app = Flask(__name__)
CORS(app)

GARMIN_EMAIL    = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")
API_SECRET_KEY  = os.environ.get("API_SECRET_KEY", "change-me")

# ── Garmin session (kept alive between requests) ───────────────────────────────

_client = None


def get_client():
    global _client
    if _client is not None:
        return _client
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        raise RuntimeError("GARMIN_EMAIL and GARMIN_PASSWORD env vars are not set.")
    log.info("Logging in to Garmin Connect as %s …", GARMIN_EMAIL)
    c = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    c.login()
    log.info("Garmin login successful.")
    _client = c
    return _client


def reset_client():
    global _client
    _client = None


# ── Cache (15 min TTL) ─────────────────────────────────────────────────────────

_cache = {}
CACHE_TTL = 900


def cached(key):
    if key in _cache:
        data, ts = _cache[key]
        now = datetime.datetime.now(datetime.timezone.utc)
        if (now - ts).total_seconds() < CACHE_TTL:
            return data
    return None


def cache_set(key, data):
    _cache[key] = (data, datetime.datetime.now(datetime.timezone.utc))


# ── Safe nested key extraction ─────────────────────────────────────────────────

def dig(data, *keys, default=None):
    for k in keys:
        if not isinstance(data, dict):
            return default
        data = data.get(k)
        if data is None:
            return default
    return data


# ── Data fetchers ──────────────────────────────────────────────────────────────

def fetch_stats(client, date_str):
    try:
        d = client.get_stats(date_str)
        return {
            "steps":              dig(d, "totalSteps"),
            "resting_heart_rate": dig(d, "restingHeartRate"),
            "max_heart_rate":     dig(d, "maxHeartRate"),
            "total_calories":     dig(d, "totalKilocalories"),
            "active_calories":    dig(d, "activeKilocalories"),
            "stress_avg":         dig(d, "averageStressLevel"),
            "body_battery_high":  dig(d, "bodyBatteryHighestValue"),
            "body_battery_low":   dig(d, "bodyBatteryLowestValue"),
        }
    except Exception as e:
        log.warning("fetch_stats failed: %s", e)
        return {}


def fetch_sleep(client, date_str):
    try:
        d   = client.get_sleep_data(date_str)
        dto = dig(d, "dailySleepDTO", default={})
        return {
            "duration_seconds":    dig(dto, "sleepTimeSeconds"),
            "score":               dig(d, "sleepScores", "overall", "value"),
            "deep_sleep_seconds":  dig(dto, "deepSleepSeconds"),
            "light_sleep_seconds": dig(dto, "lightSleepSeconds"),
            "rem_sleep_seconds":   dig(dto, "remSleepSeconds"),
            "awake_seconds":       dig(dto, "awakeSleepSeconds"),
        }
    except Exception as e:
        log.warning("fetch_sleep failed: %s", e)
        return {}


def fetch_hrv(client, date_str):
    try:
        d   = client.get_hrv_data(date_str)
        s   = dig(d, "hrvSummary", default={})
        return {
            "last_night_5_min_high": dig(s, "lastNight5MinHigh"),
            "last_night_avg":        dig(s, "lastNightAvg"),
            "weekly_avg":            dig(s, "weeklyAvg"),
            "status":                dig(s, "status"),
        }
    except Exception as e:
        log.warning("fetch_hrv failed: %s", e)
        return {}


def fetch_workouts(client, date_str):
    try:
        activities = client.get_activities_by_date(date_str, date_str) or []
        return [
            {
                "name":             a.get("activityName"),
                "type":             dig(a, "activityType", "typeKey"),
                "duration_seconds": round(a.get("duration", 0)),
                "distance_meters":  a.get("distance"),
                "calories":         a.get("calories"),
                "avg_heart_rate":   a.get("averageHR"),
                "max_heart_rate":   a.get("maxHR"),
            }
            for a in activities
        ]
    except Exception as e:
        log.warning("fetch_workouts failed: %s", e)
        return []


# ── Core logic ─────────────────────────────────────────────────────────────────

def build_response(date_str):
    hit = cached(date_str)
    if hit:
        log.info("Cache hit for %s", date_str)
        return hit

    try:
        client = get_client()
    except GarminConnectAuthenticationError:
        abort(503, "Garmin authentication failed. Check GARMIN_EMAIL / GARMIN_PASSWORD.")
    except GarminConnectTooManyRequestsError:
        abort(429, "Garmin rate limit hit. Try again in a few minutes.")
    except Exception as e:
        abort(503, f"Could not connect to Garmin: {e}")

    log.info("Fetching Garmin data for %s …", date_str)
    stats    = fetch_stats(client, date_str)
    sleep    = fetch_sleep(client, date_str)
    hrv      = fetch_hrv(client, date_str)
    workouts = fetch_workouts(client, date_str)

    result = {
        "date":    date_str,
        "steps":   stats.get("steps"),
        "sleep":   sleep,
        "hrv":     hrv,
        "heart_rate": {
            "resting_bpm": stats.get("resting_heart_rate"),
            "max_bpm":     stats.get("max_heart_rate"),
        },
        "calories": {
            "total":  stats.get("total_calories"),
            "active": stats.get("active_calories"),
        },
        "stress_avg":        stats.get("stress_avg"),
        "body_battery_high": stats.get("body_battery_high"),
        "body_battery_low":  stats.get("body_battery_low"),
        "workouts":          workouts,
    }

    cache_set(date_str, result)
    log.info("Done. Steps: %s, Workouts: %d", result["steps"], len(workouts))
    return result


# ── Auth check ─────────────────────────────────────────────────────────────────

def require_key():
    key = request.headers.get("x-api-key", "")
    if key != API_SECRET_KEY:
        log.warning("Invalid API key rejected.")
        abort(401, "Invalid API key.")


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "streakAI-garmin-server"})


@app.get("/garmin/today")
def today():
    require_key()
    date_str = datetime.date.today().isoformat()
    return jsonify(build_response(date_str))


@app.get("/garmin/date/<date_str>")
def by_date(date_str):
    require_key()
    try:
        datetime.date.fromisoformat(date_str)
    except ValueError:
        abort(400, "Invalid date format. Use YYYY-MM-DD.")
    return jsonify(build_response(date_str))


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
