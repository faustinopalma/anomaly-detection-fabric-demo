# tools/

Python scripts for Fabric-side provisioning (Eventstream source/destination
creation, `.kql` execution). The simulator itself has been moved into two
dedicated folders:

- [`../simulator-local`](../simulator-local) — local dev/debug runs
- [`../simulator-cloud`](../simulator-cloud) — always-on deployment on Azure Container Apps

## Telemetry simulator (legacy section, now in `simulator-local/`)

`simulate_machines.py` sends synthetic measurements in real time from N
industrial machines (CNC / press / motors) to the Fabric Eventstream
`es_machines`, using the `azure-eventhub` SDK (the Eventstream custom
endpoint is Event Hubs compatible).

## 1. Create the Custom Endpoint in the Eventstream

`scripts/deploy.ps1` creates an empty Eventstream. To receive data from
the outside you need to add a source of type **Custom App / Custom
Endpoint**:

1. Fabric portal → workspace `anomaly-detection-dev` → open
   `es_machines.Eventstream`.
2. **+ Add source** → **Custom App** → name `sim_local` → **Add**.
3. Click the freshly created `sim_local` node → **Details** tab →
   **Event Hub** card → **SAS Key Authentication** page.
4. Copy the **Connection string-primary key** value. Format:

       Endpoint=sb://eventstream-xxxx.servicebus.windows.net/;SharedAccessKeyName=key_xxx;SharedAccessKey=xxxx;EntityPath=es_xxxx

5. Paste the string into `.env`:

       EVENTSTREAM_CONNECTION_STRING=Endpoint=sb://...

   (The `.env` file is gitignored — the connection string never lands in the repo.)

## 2. Install dependencies

From the same venv used for the deploy:

```pwsh
.\.venv\Scripts\Activate.ps1
pip install -r simulator-local/requirements.txt
```

## 3. Run

```pwsh
# Default: 5 machines, 1 sample/sec per sensor, infinite (Ctrl-C to stop)
python simulator-local/simulate_machines.py

# 10 machines, 5 samples/sec/sensor, 2 minutes, more frequent anomalies
python simulator-local/simulate_machines.py --machines 10 --rate 5 --duration 120 --anomaly-prob 0.002
```

Total throughput = `machines × sensors_per_machine (8) × rate`.

## Event schema

Every event is a single UTF-8 JSON document matching the contract defined
in [../docs/architecture.md](../docs/architecture.md) and the
`raw_telemetry_json` mapping in [../kql/01_tables.kql](../kql/01_tables.kql):

```json
{
  "machineId": "M-001",
  "sensorId":  "temperature_motor",
  "ts":        "2026-05-07T14:23:01.123456Z",
  "value":     61.842,
  "quality":   1.0
}
```

## Simulated sensors (per machine)

| sensorId             | unit  | baseline | notes                          |
|----------------------|-------|---------:|--------------------------------|
| temperature_motor    | °C    |    60    | slow drift, 5' oscillation     |
| temperature_bearing  | °C    |    55    | slow drift                     |
| vibration_axial      | g     |     0.20 | high noise                     |
| vibration_radial     | g     |     0.30 | high noise                     |
| current              | A     |    12    | 30 s oscillation               |
| spindle_rpm          | rpm   |  3000    |                                |
| pressure_hydraulic   | bar   |   120    |                                |
| power                | kW    |     8    | derived                        |

## Injected anomalies

With probability `--anomaly-prob` per sample/sensor, one of the following
is picked at random:

- **spike**: ~15 σ peak for ~0.5 s, `quality = 0.6`
- **drift**: progressive drift up to ~25 σ over 8–20 s, `quality = 0.7`
- **stuck**: frozen value for 5–15 s, `quality = 0.4`

Useful for validating the KQL-side anomaly-detection model.
