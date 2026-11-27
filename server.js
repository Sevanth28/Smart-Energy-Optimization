
const express = require("express");
const path = require("path");
const db = require("./firebase");

const app = express();
const PORT = 3000;

app.use(express.json());
// Log incoming requests
app.use((req, res, next) => {
    console.log("Request received:", req.method, req.url);

    res.on("finish", () => {
        console.log("Response status:", res.statusCode);
    });

    next();
});

// Serve your existing HTML project
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

// Validate session IDs used by the demo
function validSessionId(id) {
    return typeof id === "string" &&
        /^[a-zA-Z0-9_-]{10,80}$/.test(id);
}

// SAVE appliance quantities
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

        // Validate incoming appliance data
        for (const device of devices) {
            const id = device.deviceId;
            const quantity = device.quantity;

            if (!Object.hasOwn(DEVICE_NAMES, id)) {
                return res.status(400).json({
                    error: "Unknown device: " + id
                });
            }

            if (!Number.isInteger(quantity) ||
                quantity < 0 || quantity > 100) {
                return res.status(400).json({
                    error: "Invalid quantity for " + id
                });
            }

            quantities[id] = quantity;
        }

        const sessionRef = db.collection("sessions")
            .doc(sessionId);

        const batch = db.batch();

        // Save every known device, including quantity zero.
        // This ensures old selections are reset correctly.
        for (const [id, name] of Object.entries(DEVICE_NAMES)) {
            const deviceRef = sessionRef
                .collection("devices")
                .doc(id);

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

// LOAD previously saved appliance quantities
app.get("/api/load-devices/:sessionId", async (req, res) => {
    try {
        const { sessionId } = req.params;

        if (!validSessionId(sessionId)) {
            return res.status(400).json({
                error: "Invalid session ID"
            });
        }

        const snapshot = await db.collection("sessions")
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

app.listen(PORT, () => {
    console.log(`Server running at http://localhost:${PORT}`);
});
