# Produktiv-Setup: was gebraucht wird, damit die App wirklich läuft

Diese Seite listet **alles**, was von außen kommen muss, damit der DME Innovation
MED17 Flasher an einem echten Fahrzeug arbeitet — Firmware-Server, Seed/Key,
Interface, Profil. Was hier nicht steht, ist bereits im Programm enthalten.

> ⚠️ Zuerst [`SAFETY.md`](SAFETY.md) lesen. Nichts davon wurde bisher an echter
> Hardware erprobt.

## 1. Interface (Pflicht)

Ein **J2534-PassThru-Gerät**, z. B. Tactrix Openport 2.0, plus dessen
Herstellertreiber. Der Treiber wird *nicht* mitgeliefert, sondern deiner
verwendet — Details und Fehlersuche in [`J2534.md`](J2534.md).

```bash
med17flasher backends          # zeigt das installierte Interface
med17flasher j2534 --listen 5  # Vorabprüfung: Versionen, Batterie, Busverkehr
```

Für die **32-Bit-Treiber** (Tactrix' `op20pt32.dll`) zusätzlich ein
**32-Bit-Python** (python.org, 32-Bit-Installer). Die App findet es selbst und
schaltet automatisch auf den Helferprozess um.

## 2. Seed/Key (Pflicht für echtes Flashen)

Ohne gültigen Key verweigert das Steuergerät `SecurityAccess` — es gibt keinen
Weg daran vorbei. Vier Quellen, in der App unter **Seed/Key** wählbar:

| Quelle | Wann | Was du brauchst |
|---|---|---|
| **Vendor-DLL (32-Bit automatisch)** | Normalfall | Pfad zu deiner lizenzierten DLL, z. B. `MED1775_12_42_00.dll` |
| **Vendor-DLL über 32-Bit-Helfer** | Wenn die Automatik nicht greifen soll | dieselbe DLL + ggf. Pfad zum 32-Bit-Python |
| **Seed/Key-Server (URL)** | Mehrere Arbeitsplätze, eine Lizenz | URL des laufenden Servers (siehe unten) |
| **Seed/Key-Katalog (JSON)** | Algorithmus ist bekannt/gelöst | `seedkeys.json` |
| Aus Profil | nur Simulator | — |

Die DLL ist 32-Bit; die App ist 64-Bit. Das ist gelöst: `Vendor-DLL` lädt
zuerst direkt und weicht bei genau diesem Ladefehler auf den Helferprozess aus.

### Seed/Key-Server (optional, für mehrere Arbeitsplätze)

Ein Rechner hält die DLL, alle anderen fragen ihn:

```bash
# auf dem Rechner mit der Lizenz/DLL:
med17flasher seedkey-server --host 0.0.0.0 --port 8377 \
    --bridge C:\keys\MED1775_12_42_00.dll
```

In der App dann **Seed/Key → Seed/Key-Server** und `http://<rechner>:8377`.

> Der Server hat **keine Zugangskontrolle**. Nur im vertrauenswürdigen
> Werkstattnetz betreiben, niemals ins offene Internet stellen.

## 3. Firmware-Server (optional, aber empfohlen)

Statt `.bin`-Dateien herumzukopieren, hält ein Rechner den Katalog:

```bash
# Server starten (Repo-Ordner wird bei Bedarf angelegt):
med17flasher fileserver --root /srv/firmware-repo \
    --host 0.0.0.0 --port 8080 --token GEHEIM
```

In der App im Reiter **Flashen → Firmware vom Server**: URL und Token
eintragen, **Verbinden**, dann die gewünschte Datei **Laden**. Sie durchläuft
exakt dieselbe Prüfung wie ein lokaler Upload.

Dateien in den Katalog legen geht per CLI oder REST:

```bash
curl -X POST "http://server:8080/firmwares?filename=asw.bin&ecu=MED17.7.5&sw_version=1779032500" \
     -H "Authorization: Bearer GEHEIM" --data-binary @asw.bin
```

Der Token schützt nur **Schreib**vorgänge; Lesen ist offen. Auch dieser Dienst
gehört ins interne Netz.

## 4. Das ECU-Profil (Pflicht — der kritische Punkt)

`config/med17_7_5_med1775.yaml` bildet einen realen Ablauf ab, aber die
konkreten Werte sind eine **Vorlage**. Vor dem ersten echten Schreibvorgang
müssen bestätigt sein:

- `can.tx_id` / `rx_id` / `padding_byte`
- `security.request_seed_level` / `send_key_level`
- `routines.*` — insbesondere `erase_argument` und ob `check_memory` genutzt wird
- `memory_map` — Startadresse und Länge des zu schreibenden Bereichs
- `transfer_data_format` (0x00 = unkomprimiert; VAG nutzt teils Kompression)
- `fingerprints` — die DID-Werte deines Werkzeugs

Zwei Wege, das zu bestätigen:

1. **Aus der ODX/Herstellerdoku** abgleichen, oder
2. **Aus einer echten Mitschrift ableiten** — der schnellere Weg:

```bash
med17flasher analyze-trace flash_session.log --profile-out med1775_real.yaml
```

Das liefert Adressen, Längen, Timing **und** die aufgezeichneten
`(seed, key)`-Paare. Gelesen werden heute candump-`.log/.txt`, Vector-`.asc`
und CSV; für andere Textformate baue ich einen Parser.

## 5. Reihenfolge für den ersten Einsatz

1. `med17flasher j2534 --listen 5` — Interface und Verkabelung prüfen
2. `med17flasher scan` — Steuergerät antwortet, Sessions/DIDs lesen (nur lesend)
3. Seed/Key-Quelle setzen und `med17flasher seedkey ...` prüfen — Key kommt an
4. Profil gegen ODX/Mitschrift bestätigen
5. Erst dann: Firmware laden, **Schreiben freigeben** ankreuzen, flashen

Schritte 1–3 sind schreibfrei und können gefahrlos am Fahrzeug laufen.

## 6. Verteilung an Arbeitsplätze

Die Releases enthalten pro Betriebssystem eine fertige Datei, unter Windows
zusätzlich einen Installer. Für einen `v*`-Tag baut CI sie automatisch.

**Ohne Code-Signatur** warnt Windows SmartScreen beim ersten Start. Das
verschwindet erst mit einem Code-Signing-Zertifikat (OV oder EV, ca. 200–500 €
im Jahr) — bis dahin muss jeder Arbeitsplatz einmal „Trotzdem ausführen"
bestätigen.
