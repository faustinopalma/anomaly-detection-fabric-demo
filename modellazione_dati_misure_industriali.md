# Modellazione dati per misure industriali in Fabric Eventhouse

## Long, Wide o Ibrido — quale scegliere e perché

Questo documento confronta le tre principali strategie di modellazione dati per misure di processo provenienti da macchine industriali, con focus sull'utilizzo in Fabric Eventhouse per anomaly detection, dashboard e analisi storica. La scelta non è puramente estetica: ha impatti misurabili su performance di query, complessità delle pipeline ML, costo di manutenzione e capacità di evoluzione dello schema nel tempo.

---

## 1. Il contesto

In ambito industriale i dati di processo arrivano tipicamente come **stream di campioni puntuali**: ogni record è un valore numerico associato a un timestamp, un identificativo di macchina e un identificativo di segnale (sensore, tag OPC-UA, variabile di processo). Le caratteristiche tipiche sono:

- Volumi elevati: da decine a migliaia di sample al secondo per impianto
- Eterogeneità: macchine di tipologie diverse (presse, compressori, CNC, robot...) con segnali fisicamente non confrontabili
- Schema potenzialmente in evoluzione: nuovi sensori aggiunti nel tempo, nuove macchine installate, nuove tipologie di impianto
- Pluralità di consumer: anomaly detection, real-time dashboard, manutenzione predittiva, reportistica produttiva, integrazione con MES/ERP

La domanda "come modellare questi dati" si scompone in due assi indipendenti:

1. **Granularità del record**: una riga per misura puntuale (*long*) o una riga per istante di campionamento con un valore per ogni segnale (*wide*)?
2. **Segregazione fisica**: una sola tabella che contiene tutto, oppure tabelle separate per tipologia di macchina?

Combinando i due assi si ottengono le tre architetture pratiche descritte di seguito.

---

## 2. Tipologia A — Tabella unica in formato long con colonne di classificazione

### Struttura

```kusto
.create table measures_enriched (
    ts: datetime,
    machine_id: string,        // 'press_01', 'comp_03', 'cnc_07'
    machine_type: string,      // 'press', 'compressor', 'cnc'
    measure_type: string,      // 'temperature', 'vibration', 'pressure', 'rpm'
    unit: string,              // '°C', 'mm/s', 'bar', 'rpm'
    value: real
)
```

Una riga per ogni misura singola. Il "cosa è" il valore lo dicono le colonne `machine_type` e `measure_type`.

### Esempio di dato

| ts | machine_id | machine_type | measure_type | unit | value |
|---|---|---|---|---|---|
| 2026-05-07T10:00:00 | press_01 | press | temperature | °C | 78.5 |
| 2026-05-07T10:00:00 | press_01 | press | vibration | mm/s | 2.3 |
| 2026-05-07T10:00:00 | comp_03 | compressor | temperature | °C | 65.1 |
| 2026-05-07T10:00:00 | comp_03 | compressor | oil_pressure | bar | 4.8 |

### Vantaggi

- **Schema fisso e stabile**: aggiungere una nuova tipologia di sensore o di macchina non richiede DDL. Si aggiungono nuove righe con valori diversi nelle colonne di classificazione, niente altro.
- **Pipeline di ingestion unica**: un solo Eventstream → una sola tabella → una sola update policy di enrichment. Bassa complessità operativa.
- **Filtri semplici e potenti**: `where machine_type == 'press'` isola un sottoinsieme; con i giusti indici e partition policy è efficiente.
- **Ideale per analisi univariate**: `series_decompose_anomalies` con `make-series ... by (machine_id, measure_type)` produce serie omogenee senza alcuna preparazione.
- **Compatibilità con strumenti standard**: il detector nativo di Fabric (Anomaly Detector item) si aspetta esattamente questa forma — una colonna numerica, un timestamp, una colonna di group by.

### Svantaggi

- **Inadatto a modelli multivariati**: un modello che usa correlazioni tra sensori (es. Isolation Forest su `temp + vib + press`) ha bisogno di vedere queste tre feature insieme nella stessa riga. Sul long pivottare a runtime è costoso e poco pratico.
- **Volumi più alti**: ogni istante di campionamento produce N righe (una per segnale) invece di una sola con N colonne. Storage e ingestion proporzionalmente maggiori.
- **Le statistiche aggregate sulla colonna `value`** non hanno senso fisico se non filtrate per `(machine_type, measure_type)`. Il valore medio di `value` su tutta la tabella è privo di significato.
- **Caching e retention uniformi**: stessa tabella = stessa policy. Non puoi conservare i dati delle macchine critiche più a lungo delle altre senza partizionamento avanzato.

### Quando ha senso

- Univariata pura: una metrica per volta, modelli indipendenti per `(machine_id, measure_type)`
- Numero elevato di tipologie di sensore in evoluzione
- Strumenti no-code o low-code (Anomaly Detector item, KQL nativo) come motore principale
- Team piccolo che non vuole gestire molte tabelle e pipeline

---

## 3. Tipologia B — Tabelle separate per tipologia di macchina in formato wide

### Struttura

```kusto
.create table measures_press (
    ts: datetime,
    machine_id: string,
    temp: real,
    vib: real,
    press: real,
    rpm: real,
    cycle_count: long
)

.create table measures_compressor (
    ts: datetime,
    machine_id: string,
    temp: real,
    oil_pressure: real,
    current: real,
    duty_cycle: real
)

.create table measures_cnc (
    ts: datetime,
    machine_id: string,
    spindle_temp: real,
    axis_load_x: real,
    axis_load_y: real,
    axis_load_z: real,
    feed_rate: real
)
```

Una riga per istante di campionamento, una colonna per ogni segnale, una tabella per ogni tipologia di macchina.

### Esempio di dato (tabella `measures_press`)

| ts | machine_id | temp | vib | press | rpm |
|---|---|---|---|---|---|
| 2026-05-07T10:00:00 | press_01 | 78.5 | 2.3 | 152.0 | 1200 |
| 2026-05-07T10:00:01 | press_01 | 78.6 | 2.4 | 152.3 | 1198 |
| 2026-05-07T10:00:00 | press_02 | 81.2 | 3.1 | 148.7 | 1180 |

### Vantaggi

- **Forma nativa per modelli multivariati**: un Isolation Forest, autoencoder, gradient boosting addestrato su correlazioni tra sensori riceve già le feature pronte all'uso, senza pivot a runtime.
- **Schema autodocumentante**: chi guarda la tabella vede subito quali sono i sensori di una pressa. Niente lookup di `measure_type` per capire cosa significa una riga.
- **Policy fini per tipologia**: caching, retention, partitioning, sharding indipendenti. Le presse possono avere caching 30 giorni, i compressori 7, i CNC 90, ognuno secondo le proprie esigenze.
- **Pipeline di scoring naturali**: una update policy di anomaly detection per tabella, ognuna con il suo modello custom dedicato. Pulizia operativa.
- **Performance di scan ottimali**: nessun filtro `where machine_type == ...` da applicare, nessuna riga "non rilevante" da scartare.

### Svantaggi

- **Schema rigido**: aggiungere un nuovo segnale a una tipologia esistente richiede DDL (`alter table add column`), test della retrocompatibilità delle query, eventuale backfill.
- **Esplosione di tabelle**: con 10 tipologie di macchina si hanno 10 tabelle di misure + 10 di anomalie + 10 update policy + 10 funzioni di scoring. Manutenzione moltiplicata.
- **Pipeline di ingestion più complesse**: serve un routing per tipologia all'ingresso, che può vivere in Eventstream (con destinazioni multiple) o in update policy a partire dalla tabella raw.
- **Nuova tipologia di macchina = progetto**: non basta aggiungere righe, bisogna creare tabelle, policy, modelli, dashboard.
- **Incompatibilità con Anomaly Detector item nativo** se i sensori sono molti, perché lo strumento si aspetta una singola colonna numerica.

### Quando ha senso

- Numero limitato e stabile di tipologie di macchina (tipicamente meno di 10)
- Modelli ML multivariati come motore principale dell'anomaly detection
- Esigenze diverse di SLA, retention o caching tra tipologie
- Team strutturato con processi DDL ben governati

---

## 4. Tipologia C — Architettura ibrida (medallion)

### Struttura

L'idea è non scegliere tra A e B, ma **costruire entrambi gli strati** in modo che ognuno serva il consumer giusto.

```
┌─────────────────────┐
│  measures_raw       │  ← Bronze: ingestion grezza, long, immutabile
│  (long, no enrich)  │     così come arriva dall'Eventstream
└──────────┬──────────┘
           │ update policy con lookup su machines_dim
           ▼
┌─────────────────────┐
│  measures_enriched  │  ← Silver: long arricchito con
│  (long, classified) │     machine_type, measure_type, unit
└──────────┬──────────┘
           │ update policy con pivot per tipologia
           ▼
┌─────────────────────┐
│  measures_press_w   │  ← Gold: wide per tipologia,
│  measures_compr_w   │     pronto per i modelli multivariati
│  measures_cnc_w     │
└──────────┬──────────┘
           │ update policy con scoring (Python plugin)
           ▼
┌─────────────────────┐
│  anomalies          │  ← output unificato in formato long,
│  (long, unified)    │     consumato da Activator e dashboard
└─────────────────────┘
```

### Componenti

**`measures_raw`** — la tabella alimentata dall'Eventstream così com'è oggi, in formato long e senza colonne di classificazione. Non si tocca; è il sorgente immutabile.

**`machines_dim`** — tabella di dimensione (anagrafica) con il mapping `machine_id → machine_type, measure_type, unit, nominal_min, nominal_max, plant`. Mantenuta a parte, popolata da Excel o da master file aziendale.

**`measures_enriched`** — versione arricchita di `measures_raw` con le colonne di classificazione, alimentata da una update policy che fa il `lookup` con `machines_dim`. Resta in formato long. È lo strato Silver.

**Tabelle wide per tipologia** (`measures_press_wide`, `measures_compressor_wide`, ...) — alimentate da update policy che fanno il pivot a partire da `measures_enriched`, una volta sola in ingestion. È lo strato Gold ottimizzato per ML.

**`anomalies`** — tabella unificata in formato long che riceve gli output di tutti i modelli (uno per tipologia di macchina), con colonne `machine_type`, `model_version`, `score`, `is_anomaly`, ecc. Single source of truth per Activator e dashboard.

### Esempio: update policy di enrichment

```kusto
.create-or-alter function EnrichMeasures() {
    measures_raw
    | lookup kind=leftouter machines_dim on machine_id
    | project ts, machine_id, machine_type, measure_type, unit, value
}

.alter table measures_enriched policy update 
@'[{"IsEnabled": true, "Source": "measures_raw", "Query": "EnrichMeasures()", "IsTransactional": false}]'
```

### Esempio: update policy di pivot per le presse

```kusto
.create-or-alter function PivotPress() {
    measures_enriched
    | where machine_type == 'press'
    | summarize 
        temp  = anyif(value, measure_type == 'temperature'),
        vib   = anyif(value, measure_type == 'vibration'),
        press = anyif(value, measure_type == 'pressure'),
        rpm   = anyif(value, measure_type == 'rpm')
        by ts = bin(ts, 1s), machine_id
}

.alter table measures_press_wide policy update 
@'[{"IsEnabled": true, "Source": "measures_enriched", "Query": "PivotPress()", "IsTransactional": false}]'
```

### Vantaggi

- **Ogni consumer ha la forma giusta per il suo lavoro**: il detector univariato e le query KQL native lavorano su `measures_enriched` (long), i modelli multivariati lavorano sulle tabelle wide per tipologia, la dashboard di alert lavora su `anomalies` (long unificato).
- **Disaccoppiamento dal sorgente**: se domani il team di ingestion sistema le colonne mancanti, sparisce solo lo strato di lookup; il resto della pipeline non cambia.
- **Single source of truth per gli alert**: una sola tabella `anomalies` da osservare con Activator, indipendentemente da quanti modelli/tipologie ci sono dietro.
- **Evoluzione graduale**: si parte con `measures_enriched` + Anomaly Detector item nativo, e si aggiungono le tabelle wide e i modelli custom solo quando servono. La complessità si paga solo se serve.
- **Riusabilità trasversale**: l'enrichment non è specifico dell'anomaly detection. Lo riusa Power BI per le dashboard, Lakehouse per la reportistica, e qualunque altro consumer futuro.

### Svantaggi

- **Più tabelle da governare**: tre strati invece di uno. Bisogna documentare cosa è autoritativo e cosa è derivato.
- **Latenza aggiuntiva tra strati**: ogni update policy aggiunge qualche secondo di delay. Per i casi d'uso quasi real-time tipici (decine di secondi) è trascurabile, ma va considerato.
- **Storage moltiplicato**: stessi dati in più forme. In Eventhouse il costo è contenuto grazie alla compressione, ma non è zero. Si compensa con retention policy aggressive sugli strati derivati (es. 7 giorni hot, lasciando lo storico solo nel raw).
- **Disciplina necessaria sulla `machines_dim`**: se l'anagrafica non è aggiornata, l'enrichment lascia righe con `machine_type` null e l'intera catena downstream le perde. Va monitorata.

### Quando ha senso

- Schema sorgente non ottimale e non immediatamente modificabile
- Mix di consumer con esigenze diverse (univariata, multivariata, dashboard, reportistica)
- Volumi che giustificano la materializzazione anziché il pivot a runtime
- Team che vuole separare le responsabilità tra strati (data engineering sul Silver, data science sul Gold)

---

## 5. Confronto sintetico

| Criterio | A — Long unica | B — Wide per tipologia | C — Ibrido |
|---|---|---|---|
| Complessità iniziale | Bassa | Media | Alta |
| Numero di tabelle | 1 | N tipologie | 1 + 1 + N + 1 |
| Adatto a univariata (KQL nativo) | Ottimo | Scomodo | Ottimo (su Silver) |
| Adatto a multivariata (ML custom) | Scomodo | Ottimo | Ottimo (su Gold) |
| Compatibile Anomaly Detector item | Sì | No (sensori multipli) | Sì (su Silver) |
| Evoluzione schema (nuovo sensore) | Zero impatto | DDL su tabella | DDL solo sul Gold interessato |
| Evoluzione (nuova tipologia macchina) | Zero impatto | Nuova tabella + policy | Nuovo Gold + policy, Silver invariato |
| Retention/caching differenziato | Difficile | Facile | Facile (sui Gold) |
| Single source of truth alert | Sì | Va costruita | Sì (`anomalies`) |
| Disaccoppiamento dal sorgente | No | No | Sì |
| Storage overhead | Minimo | Minimo | Moderato |

---

## 6. Raccomandazione

Per il caso d'uso descritto — misure industriali da macchine eterogenee, sorgente attuale in formato long senza colonne di tipologia, obiettivo di anomaly detection multivariata in quasi real-time — la scelta consigliata è la **Tipologia C — architettura ibrida**.

Le ragioni principali:

1. **Non richiede modifiche al sorgente**. Lo schema attuale, ancorché sub-ottimale, viene assorbito senza traumi. Il debito di modellazione resta documentato ma non blocca il progetto.

2. **Permette di servire entrambi i pattern di anomaly detection**. Le funzioni native KQL e l'Anomaly Detector item lavorano sul Silver in formato long; i modelli custom multivariati (Isolation Forest, autoencoder, ecc.) lavorano sui Gold wide per tipologia. Nessun compromesso obbligato.

3. **Scala con l'eterogeneità delle macchine**. Aggiungere una nuova tipologia di macchina significa aggiungere una nuova tabella Gold e una nuova update policy di pivot, senza toccare il Silver né il sorgente. Aggiungere una nuova macchina di tipologia esistente è zero-DDL: basta aggiornare `machines_dim`.

4. **Single source of truth per l'alerting**. La tabella `anomalies` raccoglie gli output di tutti i modelli, indipendentemente dalla tipologia di macchina. L'Activator ha una sola fonte da osservare, le dashboard una sola tabella da interrogare, il drift monitoring un solo posto da analizzare.

5. **Evoluzione incrementale**. Non si è obbligati a costruire tutto in una volta. Si può partire dal solo Silver con Anomaly Detector item per validare l'approccio, e introdurre i Gold + modelli custom solo per le tipologie che lo richiedono. La complessità si paga proporzionalmente al valore che si ottiene.

6. **Allineamento con pattern medallion**. La struttura raw → enriched → tipologia → anomalies è esattamente il pattern Bronze/Silver/Gold che Fabric promuove come standard. Documentazione, tooling e best practice sono allineati.

### Quando deviare dalla raccomandazione

- **Verso A** se l'anomaly detection sarà esclusivamente univariata e si vuole minimizzare lo sforzo di setup. È la scelta giusta se l'obiettivo è "alert su singolo sensore fuori soglia adattiva" e non interessa la correlazione tra sensori.

- **Verso B** se le tipologie di macchina sono pochissime (due o tre) e non si prevede crescita, e si vuole massimizzare la pulizia operativa. Adatto a contesti con un solo prodotto industriale e processi molto stabili.

---

## 7. Schema implementativo della soluzione consigliata

Ordine consigliato di implementazione:

1. **Anagrafica `machines_dim`** — tabella di dimensione popolata da master file. Anche manuale all'inizio, va bene.
2. **`measures_enriched`** + update policy di lookup — strato Silver. Da qui in poi tutti i nuovi consumer puntano qui.
3. **Anomaly Detector item nativo su `measures_enriched`** — primo motore di anomaly detection univariata, in produzione in pochi giorni. Permette di validare l'approccio mentre il resto si costruisce.
4. **Tabella `anomalies` unificata** — schema condiviso tra detector nativo e modelli custom futuri. Activator agganciato qui.
5. **Una tabella Gold wide per la tipologia di macchina più critica**, scelta in base a valore di business o a volume di anomalie. Update policy di pivot.
6. **Modello custom multivariato sulla prima Gold** — notebook di training, tabella `models`, funzione di scoring, update policy che scrive in `anomalies`.
7. **Replica del pattern (5) e (6) sulle altre tipologie**, una alla volta.
8. **Drift monitoring e retraining schedulato** — solo dopo che il sistema è in produzione e ha accumulato qualche settimana di osservazione.

In parallelo, indipendentemente dall'avanzamento tecnico, vale la pena aprire una discussione con il team che gestisce l'ingestion per portare le colonne `machine_type` e `measure_type` al sorgente. È un debito che conviene saldare nel medio termine, anche se l'architettura ibrida lo rende non bloccante nel breve.
