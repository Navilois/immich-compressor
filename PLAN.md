# Plan: `immich-compressor` — Workflow-Webhook + Post-Processing-Service (Variante B)

**Ziel:** Ein eigenständiger Dienst, der von einem Immich-Workflow per Webhook angestoßen wird, das
Original eines neu hochgeladenen Assets herunterlädt, mit ffmpeg/jpegli komprimiert, als neues Asset
hochlädt, alle übertragbaren Verknüpfungen mitnimmt und das Original kontrolliert in den Trash legt.

**Zielumgebung:** Immich `v3.1.0` (Release 2026-07-29). Der Container hier hat **2 vCPU / 8 GiB /
4096 PIDs** — Nebenläufigkeit entsprechend konservativ.

**Projektwurzel:** `/home/agent/workspace/immich/`

---

## 0. Verifizierte Fakten über Immich v3 (nicht neu recherchieren, aber gegen die laufende Instanz gegenprüfen)

### Workflow-System
- Merge: PR #26727 (2026-05-18), Preview-Release in `v3.0.0`, UI unter *Utilities > Workflows*.
- **Trigger:** nur `AssetCreate` und `AssetMetadataExtraction`.
- **Filter:** `assetFileFilter` (Name/Pfad, contains/startsWith/exact/**regex**, `usePath`),
  `assetTypeFilter` (`allowedTypes`), `assetExifFilter`, `assetDateFilter`, `assetLocationFilter`,
  `assetMissingTimeZoneFilter`.
- **Actions:** `assetArchive`, `assetLock`, `assetVisibility`, `assetFavorite`, `assetAddToAlbums`,
  **`webhook`**.
- Plugins laufen als WASM (Extism) mit nur 5 Host-Funktionen (`searchAlbums`, `createAlbum`,
  `addAssetsToAlbum`, `addAssetsToAlbums`, `httpRequest`). **Kein Dateizugriff** → die gesamte
  Kompression passiert außerhalb, im eigenen Service. Das ist der Kern von Variante B.

### Webhook-Payload (aus `packages/plugin-core/src/index.ts`)
Die Action postet **exakt** dieses JSON (kein `config`, kein `workflow`-Objekt):
```jsonc
{ "type": "AssetV1", "trigger": "AssetMetadataExtraction", "data": { "asset": { ... } } }
```
`data.asset` (aus `packages/plugin-sdk/src/types.ts`, Typ `AssetV1`) enthält u. a.:
`id, ownerId, type, originalPath, fileCreatedAt, fileModifiedAt, isFavorite, checksum (sha1, Buffer),
livePhotoVideoId, originalFileName, isOffline, libraryId, isExternal, deletedAt, localDateTime,
stackId, duplicateId, visibility, isEdited` und `exifInfo` mit
`make, model, exifImageWidth, exifImageHeight, fileSizeInByte, orientation, dateTimeOriginal,
lensModel, fNumber, focalLength, iso, latitude, longitude, city, state, country, description, fps,
exposureTime, livePhotoCID, timeZone, projectionType, profileDescription, colorspace, bitsPerSample,
rating, tags (Namen, nicht IDs)`.
→ **Prüfen** wie `checksum` (Buffer) tatsächlich JSON-serialisiert ankommt (vermutlich
`{"type":"Buffer","data":[...]}` — im Zweifel ignorieren, wir brauchen ihn nicht).
Konfigurierbare Header: nur **ein** Paar `headerName`/`headerValue` → das ist unser Shared Secret.

### Relevante API-Endpunkte (v3, gegen `open-api/immich-openapi-specs.json` verifiziert)
| Zweck | Aufruf | Anmerkung |
|---|---|---|
| Original laden | `GET /assets/{id}/original` | Streamen, nicht in RAM puffern |
| Upload | `POST /assets` (multipart) | Felder: `assetData` (binary, **required**), `fileCreatedAt` (**required**), `fileModifiedAt` (**required**), `filename`, `isFavorite`, `visibility`, `duration`, `metadata` (array), `sidecarData` (binary). Optionaler Header `x-immich-checksum`. **`deviceAssetId`/`deviceId` gibt es in v3 nicht mehr** — alle älteren Tutorials sind hier falsch. |
| Upload-Antwort | `AssetMediaResponseDto` = `{ id, status }`, `status ∈ {created, duplicate}` | `duplicate` ⇒ Datei existiert bereits, **nicht** löschen, als `skipped_duplicate` markieren |
| Verknüpfungen | `PUT /assets/copy` `{sourceId, targetId, albums, favorite, sharedLinks, stack, sidecar}` | siehe Warnung unten |
| Tags | `GET /tags`, `POST /tags`, `PUT /tags/assets` `{assetIds, tagIds}` | Tags werden von `copy` **nicht** übertragen |
| Felder nachziehen | `PUT /assets/{id}` `{description, rating, latitude, longitude, dateTimeOriginal, isFavorite, visibility, livePhotoVideoId}` | |
| Marker | `GET/PUT /assets/{id}/metadata`, Body `{items:[{key: string, value: object}]}` | **freier String-Key, Objekt-Value** — perfekt als Idempotenz-Marker |
| Löschen | `DELETE /assets` `{ids:[...]}` (ohne `force`) | → Trash, rückholbar |
| Asset-Details | `GET /assets/{id}` | für `people`-Check |
| Backfill (optional) | `POST /search/large-assets` mit `minFileSize`, `type`, `takenBefore`, `withExif` | |

**Warnung — `PUT /assets/copy` kopiert nachweislich nur** (aus `server/src/services/asset.service.ts`,
Methode `copy`): `albums`, `sharedLinks`, `stack`, `favorite`, `sidecar`.
**Nicht kopiert werden:** Tags, Description, Rating, GPS, Personen/Gesichter, Memories-Zuordnung,
Kommentare/Aktivitäten, Asset-Metadata-KV. Description/Rating/GPS müssen also aus der EXIF der neuen
Datei kommen (⇒ Metadaten beim Transcode zwingend erhalten) **und** zusätzlich per `PUT /assets/{id}`
aus dem Webhook-Payload nachgezogen werden. Tags separat per `PUT /tags/assets`.

### API-Key-Berechtigungen (v3 hat granulare Permissions)
Der Key braucht mindestens: `asset.read`, `asset.download`, `asset.upload`, `asset.update`,
`asset.delete`, `asset.copy`, `tag.read`, `tag.asset` (+ `tag.create`, falls fehlende Tags angelegt
werden sollen). Header: `x-api-key`. **Im README exakt so dokumentieren.**

---

## 1. Architektur

```
Immich (Workflow: AssetMetadataExtraction → Filter → webhook)
        │  POST /webhook  {type,trigger,data.asset}   Header: X-Compressor-Token
        ▼
┌─────────────────────────────────────────────────────────────┐
│ immich-compressor (FastAPI)                                 │
│  POST /webhook  → validieren, in SQLite-Queue, 202 zurück   │  ← muss sofort antworten
│  Worker (asyncio, concurrency=1..2)                         │
│    Guard → Download → Encode → Sanity-Gate → Upload         │
│    → copy → tags/fields → Marker → (verzögertes) Trash       │
│  GET  /healthz  /stats  /jobs?status=…                      │
│  POST /reprocess/{assetId}   (manuell nachziehen)           │
│  CLI: dry-run, backfill (optional), report                  │
└─────────────────────────────────────────────────────────────┘
```

**Warum Queue statt Inline-Verarbeitung:** Die `webhook`-Action läuft synchron im Immich-Job
`WorkflowAssetTrigger`. Ein 4-Minuten-ffmpeg-Lauf würde dort blockieren/timeouten. Der Endpoint macht
nur: Signatur prüfen → Job persistieren → `202 Accepted`.

**Stack:** Python 3.12, FastAPI + uvicorn, httpx (async), pydantic v2 (Settings + DTOs), SQLite via
`aiosqlite` (WAL), ffmpeg/ffprobe, `cjpegli` bzw. ImageMagick, `exiftool`. Ruff als Linter, pytest.
Typ-Hints überall, kein `Any`.

---

## 2. Zustandsmodell (SQLite `state.db`, Tabelle `jobs`)

| Spalte | Zweck |
|---|---|
| `source_asset_id` (PK) | Original |
| `state` | `queued → running → uploaded → linked → pending_delete → done` / `skipped_*` / `failed` |
| `skip_reason` | `already_compressed`, `too_small`, `wrong_type`, `no_gain`, `duplicate`, `named_people`, `edited`, `external_library` |
| `new_asset_id`, `orig_bytes`, `new_bytes`, `ratio` | Report |
| `attempts`, `last_error`, `created_at`, `updated_at`, `delete_after` | Retry/Verzögerung |

`state` + `attempts` geben Idempotenz auch nach Neustart. `PRAGMA journal_mode=WAL`,
`INSERT … ON CONFLICT(source_asset_id) DO NOTHING` beim Enqueue.

---

## 3. Verarbeitungspipeline (ein Job)

1. **Delay:** `initial_delay_seconds` (Default 300) abwarten, damit Thumbnail-/ML-/OCR-Jobs des
   Uploads durch sind. Job wird erst danach vom Worker gezogen.
2. **Guards (alles ⇒ `skipped_*`, nichts anfassen):**
   - `GET /assets/{id}/metadata` enthält Key `compressor` ⇒ **already_compressed** (Schleifenschutz).
   - `asset.isExternal == true` oder `libraryId != null` ⇒ **external_library** (nie External
     Libraries anfassen, die Dateien gehören dem User).
   - `asset.isEdited == true` ⇒ **edited** (non-destruktive Edits hängen am Original).
   - `asset.livePhotoVideoId != null` ⇒ skip (Live-Photo-Paare nicht auseinanderreißen).
   - `asset.visibility == locked` ⇒ skip.
   - `exifInfo.fileSizeInByte < min_size_bytes` ⇒ **too_small**.
   - Typ nicht in `enabled_types` ⇒ **wrong_type**.
   - Optional (`skip_if_named_people: true`): `GET /assets/{id}` → benannte Person dran ⇒ skip.
3. **Download:** `GET /assets/{id}/original` in Tempfile streamen (`tempfile.TemporaryDirectory`,
   Pfad konfigurierbar, Freiplatz vorher prüfen).
4. **Encode** (Preset aus Config, siehe §4). Metadaten müssen erhalten bleiben:
   - Video: `ffmpeg -i in -map_metadata 0 -movflags use_metadata_tags …`
   - Bild: nach dem Encode `exiftool -TagsFromFile <orig> -all:all -overwrite_original <neu>`
   - Danach `ffprobe`/`exiftool` auf der Ausgabe zur Kontrolle.
5. **Sanity-Gate** (alle Bedingungen müssen halten, sonst `skipped_no_gain` + Marker auf dem
   **Original**, damit nicht ewig neu versucht wird):
   - `new_bytes <= orig_bytes * max_ratio` (Default 0.6)
   - Datei ist dekodierbar (ffprobe exit 0, mindestens 1 Video-/Bild-Stream)
   - Auflösung identisch (bzw. == Zielauflösung, wenn Downscaling im Preset aktiv)
   - Video: Dauer ±0.5 s, Audio-Streams-Anzahl identisch
   - `dateTimeOriginal` in der Ausgabe vorhanden (sonst wandert das Asset in der Timeline)
6. **Upload:** `POST /assets` multipart mit `filename` = `<stem>.cmp<suffix>` (Marker auch im Namen,
   damit der Workflow-Regex-Filter greift), `fileCreatedAt`/`fileModifiedAt` aus dem Payload,
   `isFavorite`, `visibility`, `duration` (Video), und `metadata` direkt mitgeben:
   ```json
   [{"key":"compressor","value":{"v":1,"sourceId":"<uuid>","preset":"video-h265","ratio":0.41,"at":"<iso>"}}]
   ```
   Antwort `status == "duplicate"` ⇒ `skipped_duplicate`, Original **nicht** löschen.
7. **Verknüpfen:** `PUT /assets/copy` `{sourceId, targetId, albums:true, favorite:true,
   sharedLinks:true, stack:true, sidecar:true}`.
8. **Nachziehen:** `PUT /assets/{new}` mit `description`, `rating`, `latitude`, `longitude`,
   `dateTimeOriginal` aus `exifInfo` (nur gesetzte Felder). Tags: `exifInfo.tags` (Namen) → `GET
   /tags` mappen → fehlende ggf. `POST /tags` → `PUT /tags/assets`.
9. **Marker auf dem Original:** `PUT /assets/{old}/metadata` Key `compressor` mit
   `{"replacedBy": "<newId>"}` — falls Schritt 10 verzögert wird oder fehlschlägt.
10. **Löschen (verzögert):** `delete_after = now + retention_days` (Default 7) → State
    `pending_delete`. Ein Sweeper löscht fällige Originale via `DELETE /assets {ids:[…]}` (soft →
    Trash). Erst das Leeren des Trash gibt Platz frei — im README explizit sagen.
    `trash_original: false` ⇒ Service löscht nie, nur Report (Standard für den ersten Produktivlauf).

**Fehlerbehandlung:** Retry mit Exponential Backoff (max 3), danach `failed` + Log + `/stats`-Zähler.
Bei Abbruch nach erfolgreichem Upload aber vor `copy`: Job ist über `state` wiederaufsetzbar, jeder
Schritt idempotent (copy und tag-assign sind wiederholbar).

---

## 4. Konfiguration (`config.yaml` + Env-Overrides, pydantic-settings)

```yaml
immich:
  base_url: http://immich-server:2283/api
  api_key: ${IMMICH_API_KEY}          # nie in die Datei
  timeout_s: 120
webhook:
  token: ${COMPRESSOR_TOKEN}          # Gegenstück zu headerValue im Workflow
  header_name: X-Compressor-Token
behavior:
  dry_run: true                       # Default true! nichts hochladen, nichts löschen
  trash_original: false
  retention_days: 7
  initial_delay_seconds: 300
  concurrency: 1
  min_size_bytes: 20971520            # 20 MB
  max_ratio: 0.6
  enabled_types: [VIDEO]
  skip_if_named_people: true
presets:
  video-h265:
    match: { type: VIDEO }
    cmd: >
      ffmpeg -y -i {input} -map_metadata 0 -movflags use_metadata_tags
      -c:v libx265 -preset medium -crf 26 -tag:v hvc1
      -c:a aac -b:a 128k {output}
    suffix: .mp4
  image-jpegli:
    match: { type: IMAGE }
    cmd: cjpegli {input} {output} -q 88
    suffix: .jpg
```
Presets als Liste von `{name, match, cmd, suffix}` — Kommandos als Template mit `{input}`/`{output}`,
**Ausführung ohne Shell** (`asyncio.create_subprocess_exec` mit `shlex.split`, kein `shell=True`).
Ungültige Presets beim Start hart ablehnen (fail fast).

---

## 5. Immich-seitige Konfiguration (gehört ins README, als Copy-Paste)

Workflow (JSON-Editor in *Utilities > Workflows*):
```json
{
  "trigger": "AssetMetadataExtraction",
  "steps": [
    { "method": "immich-plugin-core#assetTypeFilter",
      "config": { "allowedTypes": ["VIDEO"] } },
    { "method": "immich-plugin-core#assetFileFilter",
      "config": { "pattern": "^(?!.*\\.cmp\\.).*$", "matchType": "regex", "usePath": false } },
    { "method": "immich-plugin-core#webhook",
      "config": { "url": "http://immich-compressor:8080/webhook", "method": "POST",
                  "headerName": "X-Compressor-Token", "headerValue": "<token>" } }
  ]
}
```
- Trigger bewusst `AssetMetadataExtraction`, **nicht** `AssetCreate`: erst danach ist `exifInfo`
  gefüllt (GPS, Tags, Rating, Description) — die brauchen wir zum Nachziehen.
- Der Regex-Filter ist ein Negativ-Lookahead, weil `assetFileFilter` kein `inverse` kennt. Er ist nur
  die erste Verteidigungslinie; die harte Schleifensicherung ist der `compressor`-Metadaten-Marker
  im Service.
- **Das exakte Schema von `POST /workflows` gegen `WorkflowCreateDto` in der OpenAPI-Spec prüfen**
  (Felder wie `name`, `type: "AssetV1"`, `enabled` kommen vermutlich dazu) und im README den real
  funktionierenden JSON-Block dokumentieren, nicht den obigen Entwurf ungeprüft übernehmen.

---

## 6. Lieferumfang

```
/home/agent/workspace/immich/
├── README.md                  # Setup, API-Key-Permissions, Workflow-JSON, Betrieb, Rollback
├── PLAN.md                    # dieses Dokument
├── pyproject.toml             # uv, ruff-Konfiguration
├── config.example.yaml
├── docker-compose.yaml        # Service; + docker-compose.test.yaml mit kompletter Immich-v3.1-Instanz
├── Dockerfile                 # python:3.12-slim + ffmpeg + exiftool + libjxl-tools/imagemagick
├── src/immich_compressor/
│   ├── __main__.py            # CLI: serve | dry-run | report | reprocess | backfill(optional)
│   ├── config.py              # pydantic-settings
│   ├── models.py              # Payload-/DTO-Modelle
│   ├── api.py                 # Immich-Client (httpx, typisiert, Retry)
│   ├── store.py               # SQLite-Jobstore
│   ├── encoder.py             # Preset-Ausführung + Sanity-Gate + exiftool
│   ├── pipeline.py            # die 10 Schritte
│   └── server.py              # FastAPI-Endpunkte
└── tests/
    ├── test_guards.py, test_encoder.py, test_pipeline.py   # unit, respx-Mocks
    └── test_e2e_live.py       # gegen die Testinstanz, per Marker abschaltbar
```

---

## 7. Umsetzungsreihenfolge

1. **Testinstanz zuerst.** Immich `v3.1.0` per `docker-compose.test.yaml` im inneren Docker hochziehen
   (Ports an die Tailscale-IP binden, nicht 0.0.0.0). Admin anlegen, API-Key mit den Permissions aus
   §0 erzeugen, 3–5 Testassets hochladen (ein Video, ein JPEG, eins mit GPS+Tags+Rating+Album).
   **Alle Annahmen dieses Plans dort verifizieren** — insbesondere: exakte Webhook-Payload (mit
   `nc -l`/Echo-Endpoint mitschneiden und als Fixture speichern), Workflow-Create-Schema,
   Upload-Feldnamen, `copy`-Verhalten, Metadata-KV.
2. Gerüst: `pyproject.toml`, Config, Store, `/healthz` — lauffähig, leer.
3. Immich-Client + Payload-Modelle, gegen die Testinstanz mit echten Calls verifiziert.
4. Encoder + Sanity-Gate isoliert testbar (`dry-run` auf einer lokalen Datei).
5. Pipeline zusammenstecken, zuerst mit `dry_run: true` (lädt nichts hoch, schreibt nur Report).
6. Ende-zu-Ende mit `dry_run: false`, `trash_original: false` an den Testassets. Danach prüfen:
   Album-Zugehörigkeit, Tags, Rating, Description, GPS, Timeline-Position, Stack, geteilte Links.
7. Trash-Sweeper + Retention, Reprocess-Endpoint, `/stats`.
8. README mit Betriebsanleitung, Rollback-Prozedur (Trash wiederherstellen), bekannten Grenzen.
9. Optional, nur wenn Zeit bleibt: `backfill`-CLI über `POST /search/large-assets`.

## 8. Bekannte Grenzen — gehören ins README, nicht wegdiskutieren

- **Die Asset-ID ändert sich.** Externe Deep-Links auf das alte Asset brechen. Kein Replace-Endpoint
  in der API.
- **Personen/Gesichter** werden für das neue Asset neu erkannt; manuell vergebene Namen können
  verloren gehen. Deshalb `skip_if_named_people` als Default `true`.
- **Mobile-Re-Upload:** Die App gleicht über Checksummen ab (`/assets/bulk-upload-check`). Ist das
  Original endgültig gelöscht, kann ein Gerät dieselbe Datei erneut hochladen — der Service
  komprimiert sie dann erneut (Marker greift nicht, weil es ein neues Asset ist). Muss in der
  Testphase mit einem echten Gerät beobachtet werden; im README als offenes Risiko benennen.
- **Platz wird erst frei, wenn der Trash geleert wird.**
- **ML-Last:** Jedes neue Asset triggert Thumbnails, Metadata, Smart Search, Faces, OCR erneut.
- **Kein Anfassen von External Libraries und Live Photos.**

## 9. Definition of Done

- `docker compose up` startet Service + Testinstanz; Workflow-JSON aus dem README funktioniert
  unverändert.
- Ein hochgeladenes Testvideo wird komprimiert, ersetzt, behält Album/Tags/Rating/Description/GPS,
  das Original liegt im Trash, ein zweiter Webhook für dasselbe Asset ist ein No-Op.
- `dry_run: true` verändert nachweislich nichts am Server.
- `ruff check` sauber, `pytest` grün, README enthält API-Key-Permissions, Workflow-JSON, Rollback.
- Am Ende ein knapper Bericht: was verifiziert wurde, welche Annahme aus §0 sich als falsch erwiesen
  hat, was offen bleibt.
