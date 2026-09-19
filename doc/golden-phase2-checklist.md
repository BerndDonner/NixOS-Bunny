# Phase 2 — manuelle Golden-Image-Checkliste

Nach `./scripts/mct-vm.py prepare-golden` die folgenden Punkte in der laufenden
Golden-VM abarbeiten:

- Google Chrome starten und Datenschutz-/Suchmaschinen-Einstellungen setzen.
- In VS Code **Arduino Maker Workshop** und **Continue** installieren.
- Arduino AVR Platform installieren.
- Vorbereiteten Sketch kompilieren und benötigte Zusatz-Plugins akzeptieren.
- Echten Upload auf Hardware durchführen.
- Echte Continue-Abfrage gegen den gehosteten Server testen.
- Optional `arduino-layout-svg` mit Okular testen.
- VM sauber herunterfahren.
- Danach `./scripts/mct-vm.py finalize-golden` ausführen.

Die Continue-Konfiguration ist bereits vor der manuellen Phase vorhanden. Falls
die Continue-Erstinstallation sie verändert, wird während der manuellen Phase die
vorbereitete zweite Kopie verwendet. `finalize-golden` kontrolliert oder ersetzt
die Continue-Konfiguration absichtlich nicht mehr.
