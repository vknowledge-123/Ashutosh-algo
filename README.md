# Borghate FastAPI Trading App � Detailed Architecture & Data Flow

This README is written in an interview-prep style: what the system does, how data flows, where it is stored, and how it is served to the UI.

---

## 1) What This App Is
A trading automation and monitoring system that:
- Receives **Chartink alerts** via webhook
- Validates rules (entry time, sector filter, trade limit)
- Places **market orders** via **Zerodha KiteConnect**
- Tracks positions in **Redis**
- Streams **live ticks** to update LTP/P&L in the dashboard

---

## 2) Core Actors / Components

### A) Chartink (Signal Source)
- Sends webhook to: `POST /webhook/chartink?user_id=1`
- Payload includes alert name and symbols

### B) FastAPI Server (`app/main.py`)
- Receives webhook
- Saves alert history to Redis
- Calls `TradeEngine` to place orders
- Broadcasts updates to UI via WebSocket

### C) Trade Engine (`app/trade_engine.py`)
- Validates alert rules
- Fetches LTP
- Places orders using KiteConnect
- Creates/updates positions
- Handles exit logic on ticks

### D) Zerodha KiteConnect
- Order placement (market)
- Positions fetch
- LTP fallback (if no tick)

### E) Kite Ticker WebSocket
- Live ticks (LTP, close, etc.)
- Used for live P&L updates

### F) Redis (`app/redis_store.py`)
- Persistent store for alerts/config/positions

### G) Dashboard (`app/static/dashboard.html`)
- Displays alerts and positions
- Updates LTP/P&L live from ticks
- Uses REST + WebSocket

---

## 3) End-to-End Data Flow (Step-By-Step)

### Step 1 � Chartink ? Webhook
Chartink sends:
```
POST /webhook/chartink?user_id=1
```
Payload includes alert name + symbols.

### Step 2 � Webhook Saved
Server immediately saves a **RECEIVED** entry in Redis:
- `alerts:{user_id}` list

### Step 3 � TradeEngine Processing
`TradeEngine.on_chartink_alert()`:
- Loads config for that alert
- Checks entry window (time)
- Checks sector filter (if enabled)
- Checks trade limit (per day per alert)
- Gets LTP (from tick or REST fallback)
- Places order via KiteConnect
- Creates a Position record

### Step 4 � Position Saved
Position saved in Redis:
- `positions:{user_id}` hash

### Step 5 � Result Broadcast
Final alert result broadcast to UI (WebSocket) so table updates instantly.

### Step 6 � Live Tick Updates
Kite Ticker sends ticks. Server forwards ticks to UI. UI updates:
- LTP column
- P&L column
- TSL if applicable

---

## 4) Persistence (Redis Keys)

| Data Type | Redis Key | Purpose |
|---|---|---|
| Alert configs | `cfg:alerts:{user}` | Stored alert rules |
| Alerts history | `alerts:{user}` | Recent alert results |
| Positions | `positions:{user}` | Active + exiting positions |
| Open trade guard | `trade:open:{user}:{symbol}` | Prevent duplicate trades |

---

## 5) Live Updates in UI

### A) REST Polling
- `/api/alerts` ? initial alert list
- `/api/positions` ? initial positions

### B) WebSocket Push
- `type: "alert"` ? new alert rows
- `type: "tick"` ? live LTP/PnL updates
- `type: "pos"` or `pos_refresh` ? position updates

UI logic:
- **LTP/PnL updates happen without re-render**
- **Rows stay stable** (history not removed)

---

## 6) Order Lifecycle (Interview View)

1. Alert received
2. Order placed
3. Position created
4. Live monitoring (target/SL/TSL)
5. Exit condition met
6. Exit order placed
7. Position marked CLOSED

---

## 7) Exit Logic
Exit reason when any of these hit:
- **TARGET**
- **STOP_LOSS**
- **TRAILING_SL**

Exit triggers update Redis and UI status.

---

## 8) Restart System Button (Backend)
- Button ? `POST /api/service/restart`
- Backend runs the configured restart command and now checks whether it succeeded
- For Linux/GCP, the command should return quickly, for example by using `systemctl --no-block`

Env vars:
```
ENABLE_SERVICE_RESTART=1
TRADING_RESTART_CMD=/bin/systemctl restart trading.service
```

Recommended for GCP/systemd:
```
ENABLE_SERVICE_RESTART=1
SERVICE_RESTART_TOKEN=change-this-to-a-long-random-value
TRADING_RESTART_CMD=/usr/bin/sudo /usr/local/bin/restart-trading-stack.sh
```

Notes:
- If your app runs as a non-root Linux user, direct `systemctl restart ...` usually fails from the web request.
- Use a root-owned helper script plus a narrow `sudoers` rule instead.
- The helper can restart one or more units, for example `trading.service redis-server.service`.

---

## 9) Important Files (Short Explanation)

- `app/main.py`
  - FastAPI routes, webhook handling, restart endpoint

- `app/trade_engine.py`
  - Trading logic, order placement, monitoring, exit

- `app/redis_store.py`
  - Redis read/write helpers

- `app/websocket_manager.py`
  - WebSocket broadcast + throttling

- `app/static/dashboard.html`
  - UI, table render, tick updates

---

## 10) Typical Interview Summary (One-Paragraph)
This system is a FastAPI-based trading engine. It ingests Chartink webhook alerts, persists them in Redis, applies user configurations (entry window, sector filter, trade limits), places market orders via Zerodha KiteConnect, and maintains positions in Redis. A Kite Ticker WebSocket provides live tick data that is streamed to the browser, where the dashboard updates LTP and P&L without full re-render. Alerts and positions are merged into a single table for stable history, and exit logic updates status when target/SL/TSL triggers.

---

## 11) Local Run (Windows)
```
py -m venv myvenv
.\myvenv\Scripts\activate
pip install -r requirements.txt
py -m uvicorn app.main:app --host 0.0.0.0 --port 8005
```

## 12) Linux systemd Run
Set in service file:
```
Environment="ENABLE_SERVICE_RESTART=1"
Environment="TRADING_RESTART_CMD=/bin/systemctl restart trading.service"
```
Then:
```
sudo systemctl daemon-reload
sudo systemctl restart trading.service
```

---

If you want this README in Hindi or want architecture diagrams, tell me � I�ll add.
