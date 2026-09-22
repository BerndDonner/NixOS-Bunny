# Manuelle Golden-Image-Checkliste

Nach `./scripts/mct-vm.py build-golden` die folgenden Punkte in der sichtbar
laufenden `golden-*.building.qcow2` abarbeiten:

- Chromium starten und Datenschutz-/Suchmaschinen-Einstellungen setzen.
- In VS Code **Arduino Maker Workshop** und **Continue** installieren.
- Arduino AVR Platform installieren.
- Vorbereiteten Sketch kompilieren und benötigte Zusatz-Plugins akzeptieren.
- Echten Upload auf Hardware durchführen.
- Echte Continue-Abfrage gegen den gehosteten Server testen.
- Optional `arduino-layout-svg` mit Okular testen.
- VM sauber herunterfahren.
- Danach `./scripts/mct-vm.py finalize-golden` ausführen.

Die Continue-Konfiguration ist bereits vor der manuellen Phase vorhanden.
`finalize-golden` kontrolliert oder ersetzt sie absichtlich nicht mehr. Beim
Start von `finalize-golden` wird die ausgeschaltete `.building`-VM zum
**geschützten** `golden-*.qcow2` promoviert. Die eigentliche Bereinigung erfolgt
nur auf einer Kopie `golden-*.finalizing.qcow2`; die Handarbeit wird dadurch
nicht in-place verändert.
