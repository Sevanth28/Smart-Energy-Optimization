
const express = require("express");
const path = require("path");
const db = require("./firebase");

const app = express();
const PORT = 3000;

app.use(express.json());

// Log incoming requests.
app.use((req, res, next) => {
    console.log("Request received:", req.method, req.url);

    res.on("finish", () => {
        console.log("Response status:", res.statusCode);
    });

    next();
});

// Serve your existing HTML project.
app.use(express.static(path.join(__dirname, "public")));

const DEVICE_NAMES = {
    fan: "Ceiling Fans",
    led: "LED Lights",
    fridge: "Refrigerator",
    ac: "Air Conditioner",
    geyser: "Water Heater",
    purifier: "Air Purifier",
    pump: "Water Pump"
};

// Validate session IDs.
function validSessionId(id) {
    return typeof id === "string" &&
        /^[a-zA-Z0-9_-]{10,80}$/.test(id);
}

// ENVIRONMENT API
// Fetch environmental data using the device's GPS coordinates.
app.get("/api/environment", async (req, res) => {
    try {
        const latitude = Number(req.query.latitude);
        const longitude = Number(req.query.longitude);

        if (
            !Number.isFinite(latitude) ||
            latitude < -90 ||
            latitude > 90 ||
            !Number.isFinite(longitude) ||
            longitude < -180 ||
            longitude > 180
        ) {
            return res.status(400).json({
                success: false,
                error: "Valid latitude and longitude are required."
            });
        }

        // Weather API.
        const weatherUrl = new URL(
            "https://api.open-meteo.com/v1/forecast"
        );

        weatherUrl.search = new URLSearchParams({
            latitude: String(latitude),
            longitude: String(longitude),
            current:
                "temperature_2m,apparent_temperature,relative_humidity_2m,shortwave_radiation,is_day,precipitation",
            hourly: "soil_moisture_0_to_1cm",
            timezone: "auto"
        }).toString();

        // Air quality API.
        const airUrl = new URL(
            "https://air-quality-api.open-meteo.com/v1/air-quality"
        );

        airUrl.search = new URLSearchParams({
            latitude: String(latitude),
            longitude: String(longitude),
            current: "us_aqi,pm2_5",
            timezone: "auto"
        }).toString();

        // Fetch both APIs simultaneously.
        const [weatherResponse, airResponse] =
            await Promise.all([
                fetch(weatherUrl),
                fetch(airUrl)
            ]);

        if (!weatherResponse.ok) {
            throw new Error(
                `Weather API error: ${weatherResponse.status}`
            );
        }

        if (!airResponse.ok) {
            throw new Error(
                `Air quality API error: ${airResponse.status}`
            );
        }

        const [weatherData, airData] =
            await Promise.all([
                weatherResponse.json(),
                airResponse.json()
            ]);

        const currentWeather = weatherData.current || {};
        const currentAir = airData.current || {};

        // Check whether required readings are available.
        const requiredWeather = [
            currentWeather.temperature_2m,
            currentWeather.apparent_temperature,
            currentWeather.relative_humidity_2m,
            currentWeather.shortwave_radiation,
            currentWeather.is_day
        ];

        if (
            requiredWeather.some(
                value => value === null || value === undefined
            )
        ) {
            throw new Error(
                "Required weather readings are unavailable."
            );
        }

        if (
            currentAir.us_aqi === null ||
            currentAir.us_aqi === undefined ||
            currentAir.pm2_5 === null ||
            currentAir.pm2_5 === undefined
        ) {
            throw new Error(
                "AQI or PM2.5 readings are unavailable."
            );
        }

        // Get the closest hourly soil moisture reading.
        let soilMoisture = 0.25;

        const hourly = weatherData.hourly;

        if (
            hourly?.time?.length &&
            hourly?.soil_moisture_0_to_1cm?.length
        ) {
            const currentTime = new Date(
                currentWeather.time || Date.now()
            ).getTime();

            let closestIndex = 0;
            let smallestDifference = Infinity;

            hourly.time.forEach((time, index) => {
                const difference = Math.abs(
                    new Date(time).getTime() - currentTime
                );

                if (difference < smallestDifference) {
                    smallestDifference = difference;
                    closestIndex = index;
                }
            });

            const value =
                hourly.soil_moisture_0_to_1cm[closestIndex];

            if (value !== null && value !== undefined) {
                soilMoisture = Number(value);
            }
        }

        // Send readings to the frontend.
        res.json({
            success: true,
            temperature: Number(
                currentWeather.temperature_2m
            ),
            feelsLike: Number(
                currentWeather.apparent_temperature
            ),
            humidity: Number(
                currentWeather.relative_humidity_2m
            ),
            solarRadiation: Number(
                currentWeather.shortwave_radiation
            ),
            isDay: Number(
                currentWeather.is_day
            ) === 1,
            precipitation: Number(
                currentWeather.precipitation || 0
            ),
            soilMoisture,
            aqi: Number(currentAir.us_aqi),
            pm25: Number(currentAir.pm2_5),
            updatedAt:
                currentWeather.time || new Date().toISOString(),
            timezone: weatherData.timezone || null
        });

    } catch (error) {
        console.error("Environment API error:", error);

        res.status(502).json({
            success: false,
            error:
                "Could not retrieve environmental readings. " +
                error.message
        });
    }
});

// SAVE appliance quantities.
app.post("/api/save-devices", async (req, res) => {
    console.log("SAVE DEVICES ROUTE REACHED");

    try {
        const { sessionId, devices } = req.body;

        if (!validSessionId(sessionId)) {
            return res.status(400).json({
                error: "Invalid session ID"
            });
        }

        if (!Array.isArray(devices)) {
            return res.status(400).json({
                error: "Devices must be an array"
            });
        }

        const quantities = {};

        // Validate appliance data.
        for (const device of devices) {
            const id = device.deviceId;
            const quantity = device.quantity;

            if (!Object.hasOwn(DEVICE_NAMES, id)) {
                return res.status(400).json({
                    error: "Unknown device: " + id
                });
            }

            if (
                !Number.isInteger(quantity) ||
                quantity < 0 ||
                quantity > 100
            ) {
                return res.status(400).json({
                    error: "Invalid quantity for " + id
                });
            }

            quantities[id] = quantity;
        }

        const sessionRef =
            db.collection("sessions").doc(sessionId);

        const batch = db.batch();

        // Save all devices, including quantity zero.
        for (const [id, name] of Object.entries(DEVICE_NAMES)) {
            const deviceRef =
                sessionRef.collection("devices").doc(id);

            batch.set(deviceRef, {
                deviceId: id,
                deviceName: name,
                quantity: quantities[id] ?? 0,
                updatedAt: new Date()
            });
        }

        batch.set(sessionRef, {
            updatedAt: new Date()
        }, { merge: true });

        await batch.commit();

        res.json({
            success: true,
            message: "Device quantities saved successfully"
        });

    } catch (error) {
        console.error("Save error:", error);

        res.status(500).json({
            error: "Could not save device quantities"
        });
    }
});

// LOAD previously saved appliance quantities.
app.get("/api/load-devices/:sessionId", async (req, res) => {
    try {
        const { sessionId } = req.params;

        if (!validSessionId(sessionId)) {
            return res.status(400).json({
                error: "Invalid session ID"
            });
        }

        const snapshot = await db
            .collection("sessions")
            .doc(sessionId)
            .collection("devices")
            .get();

        const quantities = {};

        snapshot.forEach(doc => {
            const data = doc.data();
            quantities[data.deviceId] = data.quantity;
        });

        res.json({
            success: true,
            quantities
        });

    } catch (error) {
        console.error("Load error:", error);

        res.status(500).json({
            error: "Could not load device quantities"
        });
    }
});

// Start server.
app.listen(PORT, () => {
    console.log(
        `Server running at http://localhost:${PORT}`
    );
});
