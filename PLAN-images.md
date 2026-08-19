# Plan: Bildkompression im `immich-compressor`

**Ziel:** Der Compressor komprimiert zusätzlich zu Videos auch Bilder. Harte Anforderungen:
keine Metadaten dürfen verloren gehen, die Rotation muss stimmen, ausgewogenes Verhältnis
zwischen Qualität und Kompression. Im selben Zug fällt die 20-MB-Untergrenze — auch für Video.

**Stand vor dieser Änderung:** Der Bildpfad existiert bereits zu großen Teilen —
`exiftool_copy`, `normalize_orientation`, `probe(is_still=True)`, das
EXIF-Orientation→Rotation-Mapping und der rotationsbewusste Display-Size-Gate sind gebaut und
getestet (`tests/test_encoder.py`, Abschnitt *stills*). Was fehlt, ist nicht das Encodieren,
sondern die Entscheidungslogik darum herum.

---

## 0. Verifizierte Fakten (im Image `immich-compressor:1.0.0` gemessen, nicht angenommen)

### Werkzeuge

- **ImageMagick 7.1.1-43 Q16**, gebaut mit **OpenMP(4.5)** → dimensioniert seinen Threadpool
  nach Host-Kernen und ignoriert das cgroup-Limit. Dieselbe Falle, die für x265 schon
  dokumentiert ist (`pools=2 -threads 2`), nur ungedeckelt.
- **libraw 0.21.4** → DNG/CR2/CR3/NEF/ARW werden **gelesen** (`r--`). Ein RAW landet ohne
  Formatfilter im Encoder und wird zu 8-bit-JPEG entwickelt.
- **libheif 1.19.8** → HEIC/AVIF nur lesen, **kein Schreiben**.
- Schreibbar: JPEG (libjpeg-turbo 2.1.5 — kein mozjpeg, kein jpegli), PNG. Kein WEBP-Write.
- `cjxl`/`djxl`/`jxlinfo` vorhanden, **`cjpegli` nicht** (wie im README dokumentiert).
- `exiftool` 13.25.
- Das Binary heißt in IM7 `magick`; `convert` ist nur noch ein Deprecated-Alias.

### Qualität vs. Kompression

Sweep über zwei Quellen, `-auto-orient -quality N -sampling-factor 4:2:0`:

`testinstance/media/test-image.jpg` — 3000×2000, Q=94, 4:2:0, 371 280 B

| q | bytes | ratio | SSIM | progressiv | ratio |
|---|---|---|---|---|---|
| 75 | 205 818 | 0.554 | 0.9735 | 188 794 | 0.508 |
| 80 | 230 940 | 0.622 | 0.9762 | 213 509 | 0.575 |
| 82 | 243 597 | 0.656 | 0.9776 | 223 843 | 0.603 |
| 85 | 265 986 | 0.716 | 0.9800 | 240 565 | 0.648 |
| 90 | 316 489 | 0.852 | 0.9833 | 287 080 | 0.773 |

Detailreiche Quelle — 4000×3000, Q=95, 5 821 858 B

| q | ratio | SSIM |
|---|---|---|
| 75 | 0.252 | 0.729 |
| 82 | 0.383 | 0.790 |
| 88 | 0.478 | 0.810 |

Drei Schlüsse:

1. **`-interlace Plane` (progressives JPEG) ist gratis.** `compare -metric AE` zwischen
   baseline und progressiv ist **0** — pixelidentisch, nur andere Scan-Reihenfolge derselben
   DCT-Koeffizienten. Ertrag 3–8 %.
2. **`-sampling-factor 4:2:0` im heutigen Preset ist schädlich.** Lässt man den Parameter weg,
   **erbt ImageMagick das Subsampling der Quelle** (verifiziert: 4:4:4-Quelle → 4:4:4-Ausgabe,
   auch bei q82). Fest verdrahtetes 4:2:0 halbiert bei jeder 4:4:4-Quelle die
   Chroma-Auflösung — und **kein Gate bemerkt das**.
3. **Ein fester SSIM-Boden funktioniert nicht.** Dieselbe Qualitätsstufe liefert 0.978 bzw.
   0.790, rein inhaltsabhängig. SSIM wird deshalb bewusst **nicht** als Gate verwendet.

> Nebenbefund: q82 ergibt bei `test-image.jpg` ratio 0.656 — das heutige globale
> `max_ratio: 0.6` würde es abweisen. Auch das im README dokumentierte „371 kB → 244 kB"
> (ratio 0.658) fiele durch den eigenen Gate.

### Metadaten-Erhalt

Produktionsbefehl aus `encoder.copy_metadata` gegen ein JPEG mit 39 Tags über
EXIF/GPS/XMP/IPTC (Make, Model, LensModel, DateTimeOriginal, Artist, Copyright, UserComment,
ISO, FNumber, GPS-Koordinaten inkl. Altitude/DateStamp, XMP Rating/Description/Subject/Label,
IPTC Keywords/City/Caption/By-line):

- **0 Tags fehlen, 0 unerwartet dazugekommen.**
- Genau eine Änderung: `EXIF:Orientation 6 → 1` — die beabsichtigte Normalisierung.
- **Eingebettetes EXIF-Thumbnail überlebt** byte-identisch (3039 B). Das war der
  wahrscheinlichste Verlustkandidat; er trifft nicht zu.
- Gruppen-Satz vorher/nachher identisch (`Composite EXIF ExifTool File JFIF`).

**Nicht verifiziert:** echte MakerNotes einer Kamera. `-TagsFromFile -all:all` kopiert sie
laut exiftool-Doku als Block, gemessen ist das hier nicht.

### Motion Photos — der gefährlichste Fall

Ein Samsung/Google Motion Photo ist ein JPEG mit **angehängtem MP4-Trailer**:

| | Quelle | nach dem Produktionspfad |
|---|---|---|
| Größe | 1 935 292 B | 389 697 B |
| `ftyp` (MP4 im Trailer) | **ja** | **nein — Video vernichtet** |
| `XMP:MotionPhoto` | 1 | **1 (sauber kopiert)** |
| `XMP:MicroVideo` | 1 | **1 (sauber kopiert)** |
| `identify` Frames | 1 | 1 |

**Jeder Gate dieses Entwurfs lässt den Fall durch:** Der Metadaten-Diff ist grün, gerade *weil*
die Kopie so gut ist. `max_ratio` sieht 0.20 — den besten Wert überhaupt. `min_savings_bytes`
sieht 1,5 MB gespart. Display-Size, Bittiefe, HDR, Aufnahmedatum: unverändert. Das Ergebnis ist
ein JPEG, das per Metadaten behauptet, ein Motion Photo zu sein, und keines mehr ist.

Der einzige heutige Schutz ist der `live_photo_video_id`-Guard, und der greift nur, **wenn
Immich das eingebettete Video vorher extrahiert und verknüpft hat**. Diese Annahme ist gegen
v3.1.0 nicht geprüft.

### Personen/Gesichter

`PUT /assets/copy` überträgt **keine Personen** (im README gegen v3.1.0 verifiziert,
`api.py:280`). Einziger Schutz ist `skip_if_named_people: true` — jedes Foto mit benannter
Person wird also nie komprimiert. Entscheidend ist der Zeitpunkt:

| Pfad | Guard läuft | Personen benannt? | Ergebnis |
|---|---|---|---|
| Webhook (Neuupload) | 300 s nach Upload | nein, Benennung erfolgt später manuell | Guard feuert nie; spätere Benennung landet direkt auf dem komprimierten Asset |
| Backfill (Bestand) | Jahre nach Upload | ja | Guard feuert, Foto bleibt unkomprimiert |

**Früh komprimieren hat kein Personenproblem, spät komprimieren schon.**

---

## 1. Entscheidungen

| # | Thema | Entscheidung |
|---|---|---|
| 1 | Quellformate | **Allowlist** statt Denylist |
| 2 | HEIC | **raus** — nur JPEG (`.jpg`, `.jpeg`, `.jpe`, `.jfif`) |
| 3 | Qualität | fest **q82**; zusätzlich Quell-Q-Guard: `identify %Q ≤ 85` → skip |
| 4 | Nutzenkriterium | `max_ratio` wird Katastrophennetz (Bild **0.9**); **`min_savings_bytes`** ist das eigentliche Kriterium |
| 5 | 20-MB-Grenze | `min_size_bytes` **gestrichen**, ersetzt durch abgeleitete Untergrenze `size ≥ min_savings_bytes` = **1 MiB, beide Typen** |
| 6 | Queue | **getrennte Spuren** — `asset_type`-Spalte, je eine Video- und Bild-Lane |
| 7 | Metadaten | **voller Gruppen-Diff** EXIF/GPS/XMP/IPTC, zweistufig (warn → hart) |
| 8 | Motion Photos | **erkennen und überspringen** — XMP-Marker **und** EOI-Trailer-Prüfung |
| 9 | Bestand | **kein Backfill** in diesem Change |
| 10 | Aufnahmedatum | `require_date_time_original` pro Preset, Bild **false** |
| 11 | Rollout | **direkt scharf** — Bilder erben die Video-Löschpolitik |
| 12 | Gate-Härte | **an `delete_mode` gekoppelt** — `permanent` erzwingt den harten Metadaten-Gate |

### Begründungen zu den nicht offensichtlichen Punkten

**Zu 2 (HEIC raus).** HEIC → JPEG macht die Datei größer, nicht kleiner: HEVC-Intra schlägt
JPEG bei gleicher Qualität um etwa Faktor 2. HEIC-Write gibt es im Image nicht. HEIC hat das
Problem also gar nicht, das hier gelöst wird. Wenn es später relevant wird, ist der Weg ein
eigenes Preset mit `heif-enc` — die `extensions`-Matcherei aus 1 macht das ohne Umbau möglich.

**Zu 4 (Ratio ist für Bilder die falsche Achse).** Ein Video-Encode kostet Minuten, deshalb ist
dort ein Verhältnis-Kriterium richtig. Ein Bild-Encode kostet eine Sekunde. Dann zählt nicht
das Verhältnis, sondern die absolute Ersparnis: ratio 0.75 auf 12 MB spart 3 MB, ratio 0.60 auf
371 KB spart 147 KB. Der heutige Gate nimmt die 147 KB und wirft die 3 MB weg.

**Zu 5 (die abgeleitete Untergrenze).** `min_size_bytes` hat zwei Jobs erledigt: „lohnt sich
das?" (macht `min_savings_bytes` ab jetzt besser, weil es das Ergebnis misst statt es zu raten)
und „muss ich die Datei überhaupt holen?". Für den zweiten gibt es eine Untergrenze, die kein
geratener Wert ist: **eine Datei kann nicht mehr Bytes sparen, als sie groß ist.** Ist
`fileSizeInByte < min_savings_bytes`, ist die Ablehnung mathematisch sicher — keine falschen
Negativen, kein Heuristik-Risiko, und `fileSizeInByte` steht schon im Webhook-Payload.

Ein zusätzlicher **bpp-Vorfilter** (`fileSize·8/(w·h)`, unter ~1,0 bpp ist ein JPEG bereits
effizient kodiert) wurde geprüft und **bewusst verworfen**: die Korrelation ist real (0.50 bpp →
ratio 0.603; 3.88 bpp → ratio 0.383), aber zwei Datenpunkte sind keine Kalibrierung. Ein zu hoch
gesetzter bpp-Filter überspringt stillschweigend Bilder, die sich gelohnt hätten. Der
`min_savings`-Gate kostet im Zweifel eine Sekunde CPU, der bpp-Filter kostet echte Ersparnis.

**Zu 10 (`require_date_time_original` für Bilder aus).** Die Begründung im Code lautet
*„would land wrong in the timeline"*. Für Bilder trägt sie nicht: `upload_asset` sendet
`fileCreatedAt` aus dem Quell-Asset (`pipeline.py:330`), und Schritt 8 schreibt
`dateTimeOriginal` danach explizit über die API (`pipeline.py:519`). Die Timeline-Position hängt
an zwei API-Feldern, nicht am EXIF der Datei. Der Gate würde JPEGs ohne EXIF-Datum — Scans,
Editor-Exporte, hochauflösende Screenshots — nach vollem Download und Encode abweisen und mit
einem `no_gain`-Marker dauerhaft blockieren.

**Zu 12 (Härte an `delete_mode` koppeln).** Der zweistufige Metadaten-Gate aus 7 hat eine
Lernphase, weil MakerNotes nicht verifiziert werden konnten. Mit Entscheidung 11 fällt diese
Phase mit dem endgültigen Löschen zusammen — eine Lernphase, in der man aus dem Gelernten nichts
mehr machen kann, ist keine. Die Kostenasymmetrie gibt die Richtung vor:

| | Kosten |
|---|---|
| Gate schlägt fälschlich an | Job scheitert, Original bleibt, sichtbar im `report` |
| Gate schlägt fälschlich **nicht** an | Metadaten weg, Original weg, kein Rollback außer Postgres-Backup |

Durchgesetzt wird das als Startup-Validierung im Muster von `_validate_delete_mode`, das
widersprüchliche Kombinationen heute schon ablehnt (`permanent` ohne `trash_original`,
`permanent` mit `dry_run`). Das ist der dritte Fall derselben Regel.

---

## 2. Zielkonfiguration

```yaml
behavior:
  enabled_types: [VIDEO, IMAGE]
  min_savings_bytes: 1048576   # ersetzt min_size_bytes; Vorfilter UND Gate
  max_ratio: 0.6               # bleibt der Video-Wert
  # min_size_bytes: entfernt

presets:
  video-h265:
    match: { type: VIDEO }
    # unverändert

  image-jpeg:
    match:
      type: IMAGE
      extensions: [.jpg, .jpeg, .jpe, .jfif]
    cmd: magick {input} -auto-orient -quality 82 -interlace Plane {output}
    suffix: .jpg
    exiftool_copy: true
    normalize_orientation: true
    max_ratio: 0.9
    require_date_time_original: false
    min_source_quality: 86
    timeout_s: 900
```

Gegenüber dem heute ausgelieferten `image-magick`-Preset: `-sampling-factor 4:2:0` **entfällt**,
`-interlace Plane` kommt dazu, `convert` → `magick`.

---

## 3. Codeänderungen

### `config.py`

- `Preset.match` akzeptiert zusätzlich `extensions: list[str]`; leer = jede Endung.
- `Preset` bekommt die Überschreibungen `max_ratio`, `min_savings_bytes`,
  `require_date_time_original`, `min_source_quality` (jeweils `None` = `behavior`-Wert gilt).
- `BehaviorSettings.min_size_bytes` **entfällt**, `min_savings_bytes` kommt (Default 1 MiB).
- `preset_for(asset_type)` → `preset_for(asset_type, filename)`; Endungsvergleich
  case-insensitiv, erster Treffer gewinnt.
- `Settings._validate`: `enabled_types` muss weiterhin von mindestens einem Preset bedient
  werden.
- Neue Validierung: `delete_mode == "permanent"` verlangt `metadata_verify: "strict"`.

### `encoder.py`

- `jpeg_quality(path)` — `identify -format '%Q'`, für den Quell-Q-Guard.
- `has_embedded_media(path)` — zwei Signale: XMP-Marker (`MotionPhoto`, `MicroVideo`,
  `MotionPhotoVersion`, `MicroVideoOffset`) aus exiftool **und** >4 KB Nutzdaten nach dem
  letzten JPEG-EOI-Marker `FFD9`. Die Schwelle deckt harmloses Padding ab.
- `verify_metadata(source, target)` — Diff über `-EXIF:all -GPS:all -XMP:all -IPTC:all`.
  Jedes Quell-Tag muss im Ziel mit gleichem Wert existieren. Ignoriert werden zwei Tags:
  `EXIF:Orientation` — sowohl geändert (6→1) als auch **neu hinzugekommen**, denn
  `-Orientation#=1` schreibt das Tag auch bei Quellen ohne eigenes — und `XMP:XMPToolkit`.

  > **Nachtrag aus der Umsetzung:** `XMP:XMPToolkit` war im Entwurf nicht vorgesehen. Der
  > Rauchtest gegen `testinstance/media/test-image.jpg` hat ihn gefunden:
  > `'Image::ExifTool 12.76' -> 'Image::ExifTool 13.25'`. Das Tag ist der Versionsstempel
  > des schreibenden Werkzeugs, nicht getragene Metadaten — exiftool stempelt bei jeder
  > Kopie seinen eigenen hinein. Ohne den Eintrag wäre unter `metadata_verify: strict`
  > **jedes** Bild gescheitert, dessen Quelle eine andere exiftool-Version angefasst hat.
  > Genau das Fehlerbild, gegen das die Warn-Phase gedacht war.
- `check_sanity` liest `max_ratio`, `min_savings_bytes` und `require_date_time_original` vom
  Preset, mit `behavior` als Rückfallebene; zusätzlich der `min_savings_bytes`-Gate.

### `store.py`

- Spalte `asset_type TEXT` über `_ADDED_COLUMNS` (Migrationsmechanismus existiert bereits).
- `enqueue(..., asset_type=None)` — wird `None` übergeben, leitet der Store den Typ aus
  `payload["data"]["asset"]["type"]` ab. Hält alle bestehenden Aufrufstellen intakt.
- `claim_next(types=None)` — filtert die Spur.

### `pipeline.py`

- `check_guards`: Vorfilter `fileSizeInByte < min_savings_bytes` → `TOO_SMALL`;
  Formatprüfung gegen die Preset-Allowlist → `UNSUPPORTED_FORMAT`.
- `_run_media_steps`: nach dem Download, vor dem Encode — Motion-Photo-Erkennung
  (`EMBEDDED_MEDIA`) und Quell-Q-Guard (`SOURCE_QUALITY`), beide nur für Stills.
- Nach `encoder.encode`: `verify_metadata`, Verhalten je nach `metadata_verify`.
- `Worker.start` startet typgebundene Lanes statt eines gemeinsamen Pools.

### `models.py`

- `SkipReason`: `UNSUPPORTED_FORMAT`, `EMBEDDED_MEDIA`, `SOURCE_QUALITY`.

### `Dockerfile`

```
ENV MAGICK_THREAD_LIMIT=2 MAGICK_MEMORY_LIMIT=512MiB MAGICK_MAP_LIMIT=1GiB
```

Als `ENV` statt als `-limit` im Preset, damit es auch für ein selbstgebautes Preset gilt und
nicht vergessen werden kann. Der Q16-Pixelcache liegt bei ~96 MB für 12 MP, aber bei ~800 MB für
ein 100-MP-Panorama — gegen `mem_limit: 2g`.

### `__main__.py`

- `_backfill` von `min_size_bytes` auf `min_savings_bytes` umstellen (Folgeänderung; Backfill
  bleibt laut Entscheidung 9 ungenutzt).

### Immich-seitig, manuell

`assetTypeFilter` im Workflow auf `["VIDEO", "IMAGE"]`. Der `assetFileFilter` mit
`^(?!.*\.cmp\.).*$` funktioniert für `.cmp.jpg` unverändert.

---

## 4. Nebenwirkung auf den Video-Bestand

Mit dem Wegfall von `min_size_bytes` sind Videos zwischen 1 und 20 MB erstmals Kandidaten. Die
liegen im lokalen Store als `skipped: too_small` und bekommen nie wieder einen Webhook.
`immich-compressor requeue --reason too_small --apply` holt sie. Das ist **nicht** der
Bestands-Backfill aus Entscheidung 9, sondern nur der lokale Job-Store — separate Entscheidung
beim Rollout.

`MARKER_VERSION` bleibt bewusst bei 2: Die Gates sind für Video nur strenger geworden
(`min_savings_bytes` kommt hinzu), alte `no_gain`-Verdikte bleiben damit gültig.

---

## 5. Offene Risiken

- **MakerNotes einer echten Kamera** — Testsatz war exiftool-generiert. Genau die Lücke, für die
  der Gate zweistufig gedacht war; mit Entscheidung 12 entfällt die Lernphase, sobald
  `permanent` aktiv ist. Erwartetes Fehlerbild: Jobs landen in `failed`, Originale bleiben.
- **Echte Motion-Photo-Dateien** — Testfall war ein angehängter MP4, kein Original mit korrektem
  MPF-Segment.
- **Immichs `livePhotoVideoId`-Extraktion** für eingebettete Motion-Photo-Videos — angenommen,
  nicht gegen v3.1.0 geprüft. Der EOI-Trailer-Check aus Entscheidung 8 ist die zweite
  Verteidigungslinie und hängt nicht von dieser Annahme ab.
- **Kompressionsraten** stammen aus zwei Quellen. Die Spanne 0.38–0.66 bei q82 ist plausibel,
  aber keine Kalibrierung an echtem Material.
