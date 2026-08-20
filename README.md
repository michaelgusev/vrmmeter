# VRM Meter ⚡

**VRM Meter** is an autonomous Python application that integrates with the **Victron VRM API (v2)** to monitor, store, and estimate house electricity consumption, multi-zone Day/Night billing, and physical external meter readings with AVR (Automatic Voltage Regulator) and inverter loss compensation.

---

## Key Features

1. **Victron VRM API Integration & Auto-Discovery:**
   - Connects using Victron Access Tokens (`x-authorization: Token <token>`).
   - One-click **Auto-Discovery & Import** of all sites/installations visible to the user token.
   - Automatically populates site IDs, names, and local timezones.

2. **AVR & Inverter Loss Compensation Model:**
   - Inverter self-consumption and AVR regulation cause physical utility meters outside the house to register higher consumption than reported by VRM.
   - Evaluates the user-calibrated loss formula:
     $$E_{\text{corrected}}(t) = \big(E_{\text{raw}}(t) + k_{\text{idle}}\big) \times \left(1 + k_{\text{base}} + k_{\text{voltage}} \times \frac{|V_{\text{grid}}(t) - V_{\text{nominal}}|}{V_{\text{nominal}}}\right)$$
   - Configurable nominal grid voltage $V_{\text{nominal}}$ per installation (e.g. 220V or 230V).
   - Hourly AVR idle / no-load standby loss constant $k_{\text{idle}}$ (e.g. 0.015 kWh/h) applied even when consumption is 0.
   - Flexible external grid voltage telemetry storage with automatic fallback to $V_{\text{nominal}}$ when measurements are missing.

3. **Multi-Zone Electricity Tariffs with Effective Dates:**
   - Day / Night pricing zones (e.g. Day 07:00–23:00, Night 23:00–07:00 in installation local timezone).
   - Historical electricity price schedules with `effective_from` dates.
   - Calculates monthly bill amounts for current (incomplete) and past months.

4. **External Meter Reading Estimation:**
   - Estimates external physical day and night meter counters for any arbitrary point in time (e.g., current time or 1st of month).
   - Anchors forward from the most recent preceding manual reference reading.

5. **Extensible Telemetry Store:**
   - Ingests external grid voltage and arbitrary future telemetry via JSON API (`POST /api/telemetry`) or CSV upload.
   - Automatically triggers batch loss recalculation when new voltage telemetry or tuned coefficients are provided.

6. **Autonomous Background Sync:**
   - Uses APScheduler to periodically poll VRM API for all active installations.
   - Idempotent upserts and sync history logging.

7. **Responsive Modern Web Interface:**
   - Dashboard with interactive Day/Night consumption charts, billing breakdown, and meter counters.
   - Point-in-time Meter Calculator tool with calculation audit trails.
   - Reference Readings manager.
   - Tariffs & Price History manager.
   - Grid Voltage & Telemetry viewer with CSV importer.
   - System Settings with VRM token test & site discovery.

---

## Quick Start

### 1. Install Dependencies
Using [`uv`](https://github.com/astral-sh/uv):
```bash
uv sync --all-extras
```

### 2. Run the Application
```bash
uv run python -m vrmmeter.main
```
Open your browser at: `http://localhost:8000/`

### 3. Run Automated Tests
```bash
uv run pytest -v
```

---

## Running with Podman / Docker

### Using Podman / Docker CLI
1. Build the container image:
   ```bash
   podman build -t vrmmeter:latest .
   # or with docker:
   # docker build -t vrmmeter:latest .
   ```

2. Run the container with persistent data volume:
   ```bash
   podman run -d --name vrmmeter -p 8000:8000 -v ./data:/app/data:Z vrmmeter:latest
   # or with docker:
   # docker run -d --name vrmmeter -p 8000:8000 -v ./data:/app/data vrmmeter:latest
   ```

### Using Podman / Docker Compose
```bash
podman compose up -d
# or:
# docker compose up -d
```

---

## Configuration

Settings can be customized via `.env` or the Web UI:

| Variable | Default | Description |
|---|---|---|
| `DB_URL` | `sqlite+aiosqlite:///data/vrmmeter.db` | SQLite Database URL |
| `VRM_API_BASE_URL` | `https://vrmapi.victronenergy.com/v2` | Victron VRM v2 API endpoint |
| `SYNC_INTERVAL_MINUTES` | `30` | Periodic background sync interval |
| `HOST` | `0.0.0.0` | Web server host |
| `PORT` | `8000` | Web server port |
