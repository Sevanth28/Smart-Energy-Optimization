from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="EcoGrid Optimizer API", version="1.0.0")

# Allow frontend (file://, localhost, or deployed origin) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple in-memory cache: key -> (expires_at, payload)
_CACHE: Dict[str, tuple[datetime, dict]] = {}
CACHE_TTL = timedelta(minutes=20)
CO2_KG_PER_KWH = 0.82


def cache_get(key: str) -> Optional[dict]:
    entry = _CACHE.get(key)
    if not entry:
        return None
    expires_at, data = entry
    if datetime.now(timezone.utc) > expires_at:
        _CACHE.pop(key, None)
        return None
    return data


def cache_set(key: str, data: dict) -> None:
    _CACHE[key] = (datetime.now(timezone.utc) + CACHE_TTL, data)


# --- Pydantic Schemas ---

class ApplianceInput(BaseModel):
    id: str
    name: str
    category: str = "general"  # ac, fan, led, geyser, purifier, pump, other
    quantity: int = Field(ge=1, le=100, default=1)
    watts: float = Field(gt=0, le=50000)
    start_hour: int = Field(ge=0, le=23, default=18)
    end_hour: int = Field(ge=0, le=23, default=23)
    optimization_enabled: bool = True


class HourlyEnvPoint(BaseModel):
    hour: int = Field(ge=0, le=23)
    temperature_c: float = 28.0
    humidity_pct: float = 60.0
    apparent_temp_c: float = 29.0
    solar_radiation_wm2: float = 0.0
    is_day: int = 0
    precipitation_mm: float = 0.0
    soil_moisture: float = 0.25
    pm2_5: float = 20.0


class SimulateRequest(BaseModel):
    appliances: List[ApplianceInput]
    tariff_per_kwh: float = Field(ge=0, default=8.0)
    days_per_month: int = Field(ge=1, le=31, default=30)
    # Optional custom hourly environment (e.g., from preset or manual override)
    hourly_env: Optional[List[HourlyEnvPoint]] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


# --- External API Fetchers ---

async def fetch_open_meteo_24h(lat: float, lon: float) -> List[HourlyEnvPoint]:
    cache_key = f"env24:{round(lat, 3)}:{round(lon, 3)}"
    cached = cache_get(cache_key)
    if cached:
        return [HourlyEnvPoint(**item) for item in cached["hours"]]

    forecast_url = "https://api.open-meteo.com/v1/forecast"
    aq_url = "https://air-quality-api.open-meteo.com/v1/air-quality"

    forecast_params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": (
            "temperature_2m,relative_humidity_2m,apparent_temperature,"
            "shortwave_radiation,is_day,precipitation,soil_moisture_0_to_1cm"
        ),
        "forecast_days": 2,
        "timezone": "auto",
    }
    aq_params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "pm2_5",
        "forecast_days": 2,
        "timezone": "auto",
    }

    async with httpx.AsyncClient(timeout=12.0) as client:
        f_resp = await client.get(forecast_url, params=forecast_params)
        if f_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch forecast data")
        f_data = f_resp.json().get("hourly", {})

        # Air quality is best-effort so weather still works if AQ endpoint is slow
        pm_map: Dict[str, float] = {}
        try:
            aq_resp = await client.get(aq_url, params=aq_params)
            if aq_resp.status_code == 200:
                aq_hourly = aq_resp.json().get("hourly", {})
                times = aq_hourly.get("time", [])
                pms = aq_hourly.get("pm2_5", [])
                for t, val in zip(times, pms):
                    if val is not None:
                        pm_map[t] = float(val)
        except httpx.HTTPError:
            pass

    times = f_data.get("time", [])
    if len(times) < 24:
        raise HTTPException(status_code=502, detail="Incomplete hourly forecast returned")

    def val_at(arr: list, idx: int, default: float) -> float:
        if idx < len(arr) and arr[idx] is not None:
            return float(arr[idx])
        return default

    points: List[HourlyEnvPoint] = []
    for h in range(24):
        ts = times[h]
        points.append(
            HourlyEnvPoint(
                hour=h,
                temperature_c=val_at(f_data.get("temperature_2m", []), h, 28.0),
                humidity_pct=val_at(f_data.get("relative_humidity_2m", []), h, 60.0),
                apparent_temp_c=val_at(f_data.get("apparent_temperature", []), h, 29.0),
                solar_radiation_wm2=val_at(f_data.get("shortwave_radiation", []), h, 0.0),
                is_day=int(val_at(f_data.get("is_day", []), h, 0)),
                precipitation_mm=val_at(f_data.get("precipitation", []), h, 0.0),
                soil_moisture=val_at(f_data.get("soil_moisture_0_to_1cm", []), h, 0.25),
                pm2_5=pm_map.get(ts, 18.0),
            )
        )

    cache_set(cache_key, {"hours": [p.model_dump() for p in points]})
    return points


# --- Optimization Scaling Logic (alpha_t in [0.0, 1.0]) ---

def is_hour_active(hour: int, start: int, end: int) -> bool:
    if start <= end:
        return start <= hour <= end
    # Handles overnight schedules (e.g., 22:00 to 06:00)
    return hour >= start or hour <= end


def compute_alpha(category: str, env: HourlyEnvPoint) -> float:
    cat = category.lower().strip()

    if cat in {"led", "light", "lights", "lighting"}:
        if env.is_day == 0 or env.solar_radiation_wm2 <= 10:
            return 1.0
        return max(0.15, 1.0 - (env.solar_radiation_wm2 / 600.0))

    if cat in {"ac", "air_conditioner", "air conditioner"}:
        # Cool & dry outdoor air enables low-power fan / free cooling mode
        if env.temperature_c < 24.0 and env.humidity_pct < 60.0:
            return 0.10
        cooling_delta = max(0.0, env.temperature_c - 24.0)
        humidity_factor = 1.08 if env.humidity_pct > 75.0 else 1.0
        scaled = 0.45 + min(0.55, (cooling_delta / 12.0) * 0.55 * humidity_factor)
        return min(1.0, max(0.10, scaled))

    if cat in {"fan", "ceiling_fan", "ceiling fan"}:
        if env.apparent_temp_c < 22.0:
            return 0.0
        return min(1.0, max(0.20, (env.apparent_temp_c - 22.0) / 12.0))

    if cat in {"geyser", "water_heater", "water heater"}:
        # Warmer ambient & inlet water reduces required heating boost
        factor = 1.0 - 0.015 * (env.temperature_c - 20.0)
        return min(1.0, max(0.65, factor))

    if cat in {"purifier", "air_purifier", "air purifier"}:
        if env.pm2_5 < 15.0:
            return 0.20
        if env.pm2_5 >= 75.0:
            return 1.0
        return 0.20 + ((env.pm2_5 - 15.0) / 60.0) * 0.80

    if cat in {"pump", "irrigation", "water_pump", "water pump"}:
        if env.precipitation_mm > 2.0 or env.soil_moisture > 0.35:
            return 0.0
        # Slight daytime evaporation penalty vs. cool morning/evening efficiency
        return 0.85 if env.temperature_c < 26.0 else 1.0

    # Default mild environmental efficiency for uncategorized appliances
    return 0.90


# --- Routes ---

@app.get("/api/health")
def health_check():
    return {"status": "ok", "service": "ecogrid-backend"}


@app.get("/api/geocode")
async def geocode_city(q: str = Query(..., min_length=2)):
    url = "https://geocoding-api.open-meteo.com/v1/search"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params={"name": q, "count": 5, "language": "en"})
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Geocoding service error")
    results = resp.json().get("results", [])
    return {
        "results": [
            {
                "name": r.get("name"),
                "country": r.get("country"),
                "admin1": r.get("admin1"),
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "timezone": r.get("timezone"),
            }
            for r in results
        ]
    }


@app.get("/api/weather")
async def get_weather_24h(lat: float, lon: float):
    hours = await fetch_open_meteo_24h(lat, lon)
    return {"latitude": lat, "longitude": lon, "hours": [h.model_dump() for h in hours]}


@app.post("/api/simulate")
async def simulate_energy(req: SimulateRequest):
    if req.hourly_env and len(req.hourly_env) == 24:
        env_hours = sorted(req.hourly_env, key=lambda x: x.hour)
    elif req.latitude is not None and req.longitude is not None:
        env_hours = await fetch_open_meteo_24h(req.latitude, req.longitude)
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide either 24 hourly_env points or both latitude and longitude.",
        )

    hourly_baseline_w = [0.0] * 24
    hourly_optimized_w = [0.0] * 24
    per_appliance_stats = []

    for app_item in req.appliances:
        base_watts_when_on = app_item.quantity * app_item.watts
        app_base_kwh = 0.0
        app_opt_kwh = 0.0

        for env in env_hours:
            if not is_hour_active(env.hour, app_item.start_hour, app_item.end_hour):
                continue

            alpha = compute_alpha(app_item.category, env) if app_item.optimization_enabled else 1.0
            opt_watts = base_watts_when_on * alpha

            hourly_baseline_w[env.hour] += base_watts_when_on
            hourly_optimized_w[env.hour] += opt_watts

            app_base_kwh += base_watts_when_on / 1000.0
            app_opt_kwh += opt_watts / 1000.0

        saved_kwh = max(0.0, app_base_kwh - app_opt_kwh)
        per_appliance_stats.append(
            {
                "id": app_item.id,
                "name": app_item.name,
                "category": app_item.category,
                "baseline_kwh": round(app_base_kwh, 4),
                "optimized_kwh": round(app_opt_kwh, 4),
                "saved_kwh": round(saved_kwh, 4),
                "savings_pct": round((saved_kwh / app_base_kwh * 100.0), 2) if app_base_kwh > 0 else 0.0,
            }
        )

    daily_baseline_kwh = sum(hourly_baseline_w) / 1000.0
    daily_optimized_kwh = sum(hourly_optimized_w) / 1000.0
    daily_saved_kwh = max(0.0, daily_baseline_kwh - daily_optimized_kwh)
    savings_pct = (daily_saved_kwh / daily_baseline_kwh * 100.0) if daily_baseline_kwh > 0 else 0.0

    monthly_baseline_kwh = daily_baseline_kwh * req.days_per_month
    monthly_optimized_kwh = daily_optimized_kwh * req.days_per_month
    monthly_saved_kwh = daily_saved_kwh * req.days_per_month

    return {
        "summary": {
            "daily_baseline_kwh": round(daily_baseline_kwh, 3),
            "daily_optimized_kwh": round(daily_optimized_kwh, 3),
            "daily_saved_kwh": round(daily_saved_kwh, 3),
            "monthly_baseline_kwh": round(monthly_baseline_kwh, 2),
            "monthly_optimized_kwh": round(monthly_optimized_kwh, 2),
            "monthly_saved_kwh": round(monthly_saved_kwh, 2),
            "savings_pct": round(savings_pct, 2),
            "daily_money_saved": round(daily_saved_kwh * req.tariff_per_kwh, 2),
            "monthly_money_saved": round(monthly_saved_kwh * req.tariff_per_kwh, 2),
            "daily_co2_saved_kg": round(daily_saved_kwh * CO2_KG_PER_KWH, 3),
            "monthly_co2_saved_kg": round(monthly_saved_kwh * CO2_KG_PER_KWH, 2),
        },
        "hourly": [
            {
                "hour": h,
                "baseline_w": round(hourly_baseline_w[h], 1),
                "optimized_w": round(hourly_optimized_w[h], 1),
                "saved_w": round(max(0.0, hourly_baseline_w[h] - hourly_optimized_w[h]), 1),
                "temperature_c": env_hours[h].temperature_c,
                "humidity_pct": env_hours[h].humidity_pct,
                "solar_radiation_wm2": env_hours[h].solar_radiation_wm2,
                "pm2_5": env_hours[h].pm2_5,
            }
            for h in range(24)
        ],
        "appliances": per_appliance_stats,
    }