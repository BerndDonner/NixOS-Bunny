# VM Rollout (Donnerstag)

Dieses README beschreibt die wichtigsten Startbefehle für das zentrale Rollout-Skript.

## Voraussetzungen

- Externe SSD ist eingesteckt und erreichbar  
- Rollout-Struktur liegt z.B. unter: `D:\rollout\`
- Enthalten sind mindestens:
  - `rollout.cmd`
  - `rollout.csv`
  - Ordner `images\` (mit `bunnyXX.vmdk.zst`)
  - Ordner `tools\` (mit `zstd.exe`)

## Standard-Start (Donnerstag)

Angenommen die SSD ist `D:\rollout\`:

```cmd
D:\rollout\rollout.cmd --src D:\rollout\images --tools D:\rollout\tools --csv D:\rollout\rollout.csv
```

- liest `rollout.csv`
- rollt die passenden VMs auf die angegebenen PCs aus
- prüft SHA256
- entpackt remote
- löscht anschließend die `.vmdk.zst`
- schreibt Marker-Dateien, damit spätere Durchläufe nichts kaputt machen

## Einzelnen PC testen

Praktisch zum Testen oder für verspätete Schüler:

```cmd
D:\rollout\rollout.cmd --only S40404-01 --src D:\rollout\images
```

Hinweis: `--only` filtert auf genau diesen PC-Namen aus der CSV.

## Neue Version erzwingen (Marker ignorieren)

Wenn du eine neue Version ausrollen willst, obwohl bereits Marker-Dateien existieren:

```cmd
D:\rollout\rollout.cmd --force --src D:\rollout\images
```

- ignoriert vorhandene Marker
- rollt neu aus (inkl. Löschen/Ersetzen der alten Version)
