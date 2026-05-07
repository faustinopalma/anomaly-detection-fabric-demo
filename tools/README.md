# Telemetry simulator

`simulate_machines.py` invia in tempo reale misurazioni sintetiche da N
macchine industriali (CNC / pressa / motori) all'Eventstream
`es_machines` di Fabric, usando l'SDK `azure-eventhub` (l'endpoint
custom dell'Eventstream è Event Hubs compatibile).

## 1. Crea il Custom Endpoint nell'Eventstream

`scripts/deploy.ps1` crea l'Eventstream vuoto. Per riceverere dati
dall'esterno serve aggiungere una sorgente di tipo **Custom App / Custom
Endpoint**:

1. Portal Fabric → workspace `anomaly-detection-dev` → apri
   `es_machines.Eventstream`.
2. Pulsante **+ Add source** → **Custom App** → nome `sim_local` → **Add**.
3. Clicca sul nodo `sim_local` appena creato → tab **Details** → scheda
   **Event Hub** → pagina **SAS Key Authentication**.
4. Copia il valore di **Connection string-primary key**. Ha questo formato:

       Endpoint=sb://eventstream-xxxx.servicebus.windows.net/;SharedAccessKeyName=key_xxx;SharedAccessKey=xxxx;EntityPath=es_xxxx

5. Incolla la stringa in `.env`:

       EVENTSTREAM_CONNECTION_STRING=Endpoint=sb://...

   (Il file `.env` è gitignored — la connection string non finisce nel repo.)

## 2. Installa le dipendenze

Dal venv già usato per il deploy:

```pwsh
.\.venv\Scripts\Activate.ps1
pip install -r tools/requirements-sim.txt
```

## 3. Esegui

```pwsh
# Default: 5 macchine, 1 sample/sec per sensore, infinito (Ctrl-C per fermare)
python tools/simulate_machines.py

# 10 macchine, 5 sample/sec/sensore, 2 minuti, anomalie più frequenti
python tools/simulate_machines.py --machines 10 --rate 5 --duration 120 --anomaly-prob 0.002
```

Throughput totale = `machines × sensors_per_machine (8) × rate`.

## Schema evento

Ogni evento è un singolo JSON UTF-8 conforme al contratto definito in
[../docs/architecture.md](../docs/architecture.md) e alla mapping
`raw_telemetry_json` in [../kql/01_tables.kql](../kql/01_tables.kql):

```json
{
  "machineId": "M-001",
  "sensorId":  "temperature_motor",
  "ts":        "2026-05-07T14:23:01.123456Z",
  "value":     61.842,
  "quality":   1.0
}
```

## Sensori simulati (per macchina)

| sensorId             | unità | baseline | note                          |
|----------------------|-------|---------:|-------------------------------|
| temperature_motor    | °C    |    60    | drift lento, oscillazione 5'  |
| temperature_bearing  | °C    |    55    | drift lento                   |
| vibration_axial      | g     |     0.20 | rumore alto                   |
| vibration_radial     | g     |     0.30 | rumore alto                   |
| current              | A     |    12    | oscillazione 30 s             |
| spindle_rpm          | rpm   |  3000    |                               |
| pressure_hydraulic   | bar   |   120    |                               |
| power                | kW    |     8    | derivata                      |

## Anomalie iniettate

Con probabilità `--anomaly-prob` per sample/sensore, viene scelta a caso
una di:

- **spike**: picco di ~15 σ per ~0.5 s, `quality = 0.6`
- **drift**: deriva progressiva fino a ~25 σ in 8–20 s, `quality = 0.7`
- **stuck**: valore congelato per 5–15 s, `quality = 0.4`

Sono utili per validare il modello di anomaly detection lato KQL.
