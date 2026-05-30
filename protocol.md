# PumpSpy Protocol Reverse Engineering Notes

## Device Info
- **Device ID:** `216051108087149`
- **MAC:** `c4:7f:51:8d:05:6d` (Espressif)
- **IP on lab AP:** `192.168.50.117`
- **Bearer Token:** `15e3409a-2a8c-4266-a669-bab98bc930de`

## Transport
- Plain **HTTP** (no TLS)
- Server: `206.80.104.221:8081` (pumpspy.com)
- **Important:** Device connects by IP directly — no DNS lookup. Redirect must use iptables DNAT, not DNS spoofing.
- Server software: Apache-Coyote/1.1 (Java/Tomcat)

## API Endpoints

### 1. `POST /pings`
Heartbeat + RSSI, sent every ~2 minutes.

**Request body:**
```json
[{
  "deviceid": 216051108087149,
  "utcunixtime": 1779758262000,
  "idpings_data_type": 1,
  "value": -62.0
}]
```
- `idpings_data_type: 1` = WiFi RSSI (dBm)
- `utcunixtime` is milliseconds since epoch

**Response (200 OK):**
```json
[{
  "idpings": null,
  "deviceid": 216051108087149,
  "utcunixtime": 1779758262000,
  "idpings_data_type": 1,
  "value": -62.0,
  "date_time": null
}]
```

---

### 2. `POST /bbs_json`
Pump event data — sent on motor start, stop, and fault events.

**Request body:**
```json
{
  "deviceid": 216051108087149,
  "utcunixtime": 1779758356000,
  "json": "{\"motor\":0,\"time\":118,\"mamp\":15873,\"battery_voltage\":12779,\"loaded\":12665}"
}
```

**Nested `json` field variants:**

| Event | Fields |
|-------|--------|
| Motor stop | `motor:0, time:<secs>, mamp:<mA>, battery_voltage:<mV>, loaded:<mV>` |
| Motor start | `motor:1, time:<secs>, mamp:<mA>, battery_voltage:<mV>, loaded:<mV>` |
| Motor fault | `motor_fail: 1` |
| Fault clear | `motor_fail: 0` |

**Field meanings:**
- `motor` — 0=off, 1=running
- `time` — pump run duration in seconds
- `mamp` — current draw in milliamps (e.g. 15873 = 15.87A)
- `battery_voltage` — battery/supply voltage in millivolts (e.g. 12779 = 12.78V)
- `loaded` — loaded voltage in millivolts (voltage under load)
- `motor_fail` — fault flag (1=fault, 0=cleared)

**Response (200 OK):** mirrors the request body

---

### 3. `GET /bbs_parameters/<deviceid>`
Device fetches its config parameters on startup and periodically.

**Response (200 OK):**
```json
{
  "p1": 3000,
  "p2": 12500,
  "p3": 11000,
  "p4": 10000,
  "p5": 15,
  "p6": 7000,
  "p7": 10000,
  "p8": 48,
  "p9": 20000,
  "p10": 15000
}
```
Parameter meanings TBD — likely voltage thresholds, timing, and alert levels.

---

### 4. `GET /new_firmware/<deviceid>`
OTA firmware check. Called frequently. Cloud returns RST (no new firmware available).

**Response observed:** TCP RST / connection refused when no update available.

---

## Request Headers (all endpoints)
```
Content-Type: application/json;charset=UTF-8
authorization: Bearer 15e3409a-2a8c-4266-a669-bab98bc930de
Connection: close
Host: (empty — device sends blank Host header)
```

---

## Redirect Strategy
Since the device hardcodes the IP, use iptables DNAT on the Pi:

```bash
# Redirect device's traffic to local server on port 8081
sudo iptables -t nat -A PREROUTING -i wlan0 \
  -s 192.168.50.117 \
  -d 206.80.104.221 -p tcp --dport 8081 \
  -j DNAT --to-destination 192.168.50.1:8081
```

Then run a local HTTP server on `192.168.50.1:8081` implementing the four endpoints above.
