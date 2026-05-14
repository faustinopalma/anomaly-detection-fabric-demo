# simulator-local

Local producer that emits synthetic telemetry to the `es_machines`
Eventstream (custom endpoint, Event Hubs–compatible). Use it from your
laptop for development/debugging; for the always-on production scenario
use [`../simulator-cloud`](../simulator-cloud) instead.

## Setup

```pwsh
& ".venv\Scripts\python.exe" -m pip install -r simulator-local\requirements.txt
```

Set `EVENTSTREAM_CONNECTION_STRING` in `.env` (see `.env.example`).

## Run

```pwsh
# 5 machines, 2 samples/s/sensor, 60 seconds
python simulator-local\simulate_machines.py --machines 5 --rate 2 --duration 60

# infinite (Ctrl-C to stop), with slightly more frequent anomalies
python simulator-local\simulate_machines.py --machines 10 --rate 5 --duration 0 --anomaly-prob 0.002
```

See `python simulator-local\simulate_machines.py --help` for every flag.
