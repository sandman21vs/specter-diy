# ESP32-P4: PIN-Dialog und Button-Flackern

Umsetzungsauftrag für **GPT-6 Luna, Reasoning xHigh**. Stand: 30.09.2026.
Dieser Branch enthält zunächst nur den Plan; die Fehler wurden noch nicht auf Hardware behoben.

## 1. Arbeitsort und verbindliche Basis

- Repository des Nutzers: `C:\Users\finnn\Documents\GitHub\specter-diy`.
- Implementierungs-Worktree: `C:\Users\finnn\Documents\GitHub\specter-diy\.codex-worktrees\esp32p4-ui-fixes`.
- Branch: `fix/esp32p4-modal-button-flicker`.
- Basis: [sandman21vs/specter-diy, esp32-p4-port](https://github.com/sandman21vs/specter-diy/tree/esp32-p4-port), exakt `870767d0e6a807fec5f7a1b473fe9319e273d0d7`.
- Geflashtes ZIP: `C:\Users\finnn\Downloads\Telegram Desktop\specter-diy-wave_43-v0.1.0-alpha.1.zip`.
- ZIP-SHA256: `67b6609e7c22f7a827231c137380fdf4500aa31273016a71f00180dcf1e6c52a`.
- Alle vier enthaltenen Images wurden erneut gegen `SHA256SUMS.txt` geprüft: stimmen überein.
- ZIP-README nennt genau diese Quellbasis und Waveshare **ESP32-P4-WIFI6-Touch-LCD-4.3-C**. Laut vorherigem Flash-Protokoll: Chip v1.3, COM5; Port vor erneutem Flash prüfen.

Der ursprüngliche Checkout hat umfangreiche fremde Änderungen, darunter einen bereits gestagten Mnemonic-Edit. Er steht wieder auf `codex/specter-web-simulator`. Dort nichts resetten, stashen, committen oder durch den ESP32-Port überschreiben. Der zusätzlich vorbereitete lokale Branch `codex/esp32p4-ui-fix-plan` zeigt auf dessen ursprünglichen HEAD und ist nicht die Implementierungsbasis.

Vor Arbeitsbeginn das `AGENTS.md` im Haupt-Repository lesen. Dessen Veröffentlichungsregel gilt auch für diese Arbeit: unmittelbar vor Push, PR oder anderer Veröffentlichung ausdrückliche Nutzerbestätigung einholen. Lokales Implementieren, Bauen und Prüfen ist der nächste Auftrag; dieser Plan löst keine Veröffentlichung aus.

## 2. Ziel und Abgrenzung

1. Board starten, Next drücken, PIN eingeben: der Loader zeigt `Processing...` und `Verifying PIN code...` vollständig in einem ausreichend großen, sauberen Dialog.
2. Buttons zeigen einen stabilen Pressed-/Released-Zustand ohne sichtbare Streifen, Zwischenbilder oder Flackern; ein Tap löst genau eine Aktion aus.
3. Marcos Render-Architektur als Grundlage auf den ESP32-P4 übertragen. Keine F469-Register, Speicheradressen oder HAL-Aufrufe übernehmen.

PIN-Verifikation, Versuchszähler, Keystore, Partitionierung, Funkzustand und Sicherheitsprofil bleiben fachlich erhalten. Für diese beiden Fehler weder kompletten UI-Neubau noch neue Slide-Animationen entwickeln.

## 3. Befunde und noch zu bestätigende Ursachen

### PIN-Dialog

In der tatsächlich geflashten Basis nutzt `src/gui/components/modal.py` bereits LVGL 9:

- `lv.obj` als Dialog, Breite 400 px, Label-Breite 360 px, Padding 20 px.
- Position `TOP_MID` mit y=200.
- `set_text()` ruft erst `label.set_text(text)` auf, liest unmittelbar danach `label.get_height()` und setzt Dialoghöhe auf diesen Wert +40.
- `src/gui/screens/screen.py::show_loader()` übergibt zusätzliche Leerzeilen und ruft anschließend zweimal `update()` auf.

**Arbeitshypothese:** LVGL aktualisiert Textmaße verzögert; die Höhe wird aus dem alten Layout übernommen. Die späteren Updates korrigieren die bereits fest gesetzte Dialoghöhe nicht automatisch. Zusätzlich Theme, Textfarbe, Parent-Maße und Zentrierung prüfen. Die Hypothese ist durch Code begründet, noch nicht durch Messung auf der Hardware bewiesen.

### Flackern

`ports/esp32p4/components/lvgl_p4/lv_p4_hal.c::tft_init()` registriert genau einen Fullscreen-Puffer in `LV_DISPLAY_RENDER_MODE_DIRECT`. Dieser ist zugleich der aktive DPI-Scanout-Puffer. `tft_flush()` ignoriert `px_map`, ruft `p4board_flush(y,height)` auf und meldet sofort `lv_display_flush_ready()`.

`ports/esp32p4/components/p4board/display.c` konfiguriert `.num_fbs = 1`, RGB565 und `.flags.use_dma2d = true`. `p4board_flush()` übergibt eine Bildschirmzeile/-region und den Basiszeiger des Framebuffers an `esp_lcd_panel_draw_bitmap()`.

**Arbeitshypothese:** CPU/LVGL und Display-DMA greifen gleichzeitig auf den sichtbaren Puffer zu; Cache-Commit und Scanout können Zwischenzustände zeigen. Vor Änderung zusätzlich prüfen: tatsächliche IDF-5.5.5-Semantik für framebuffer-interne Zeiger, Quelloffset bei y>0, DMA-Abschluss, Cache-Kohärenz, Touch-Release und eventuell mehrfach laufende LVGL-Handler. `px_map` und Zeilenoffset dürfen nach einem Pufferwechsel nicht weiter ignoriert werden.

## 4. Vergleich mit Marcos Lösung

Quellen, am 30.09.2026 über GitHub API gelesen:

- [PR #18](https://github.com/Schnuartz/specter-diy/pull/18): F469-RAM-/Build-Reparatur mit einem DMA2D-Rechtecktransfer je Flush; zusätzliche QSPI-/Storage-Änderungen sind hier nicht relevant. PR-Head `42121dce763177c8c9cf592d74bbd59d2168fbf5`.
- [Marco: increase_animation_performance](https://github.com/maggo83/specter-playground/commits/increase_animation_performance/), Head `aed3a9f978080af4929a0df099f61b0d301fecb0`.
- `cd0f385ec09baa31ce2a4517c6926706025eb735`: gebündelter asynchroner 2D-Transfer, Completion-Callback, Render-Analyse.
- `bd9d8b59e4727f99ed61c2fa53fe5b63ab59e70d`: RGB565-Drawbuffer.
- `f563b301d14d611e81e20151ae5bc3f68da76b15`: Hardware-Compositor mit drei SDRAM-Puffern.
- F469-Submodul am Marco-Head: `miketlk/f469-disco`, `d35881029c3bf070f6741c3980ebe7f24c899384`.
- Relevante Referenzdatei: `usermods/udisplay_f469/lv_stm_hal/lv_stm_hal.c`; daneben `display.c` und die Python-Feature-Probes für `udisplay.transition`.

**Wichtige Grenze:** Der gelesene Marco-Treiber nutzt außerhalb von Transitionen weiterhin einen sichtbaren Live-Puffer und erwähnt verbleibendes Tearing bei normalen LVGL-Rechtecken. Drei Puffer für Animationen sind deshalb kein automatischer Fix für normale Button-Updates.

Übernehmen: klar definierter Pufferbesitz, gebündelte Transfers, genau eine Completion-Meldung, vollständige Frames vor Präsentation, Synchronisation an einem tatsächlich geeigneten Display-Ereignis, Simulator-Fallback per Capability-Prüfung. Für Button-Updates den aktiven Scanout-Puffer zusätzlich vor Schreibzugriffen schützen.

## 5. Benötigte Infrastruktur

| Ebene | Bestand und Aufgabe |
|---|---|
| App | MicroPython, eingefrorene Python-Module aus `src/`; GUI über LVGL 9 |
| GUI-Scheduling | `src/gui/core.py`, `src/gui/async_gui.py`, `ports/esp32p4/lib/display.py`, C-Modul `moddisplay.c` |
| LVGL-Anbindung | `ports/esp32p4/components/lvgl_p4/lv_p4_hal.c`, `.h`, `lv_conf.h` |
| Display/BSP | `ports/esp32p4/components/p4board/display.c`, `p4board.h`, `modp4board.c`, `board_config.h` |
| Hardware | 480×800 ST7701 MIPI DSI, GT911-Touch; laut Port-Doku 32 MB PSRAM, logisches Flash-Layout 16 MB |
| Toolchain | MicroPython `8cf130db34`, ESP-IDF 5.5.5 aus Bootloader-Abhängigkeit, RISC-V-Compiler, passendes `mpy-cross` |
| Build | Board `WAVESHARE_P4_43`; Pre-revision-3-Konfiguration für Chip v1.3 erhalten |
| Transport | Windows-Python/esptool auf COM5 oder Kern Custom ZIP Bundle |
| Nachweise | Build-Log, Versions-/Submodul-SHAs, Image-Hashes, Vorher/Nachher-Video und Serial-Log ohne PIN/Secrets |

WSL Ubuntu und Docker-Befehle sind vorhanden; eine fertige ESP-IDF-Umgebung wurde nicht geprüft. Build vorzugsweise unter WSL Ubuntu ausführen, Flash über Windows. Damit ist keine USB-Durchreichung nach WSL erforderlich. Build auf `/mnt/c/...` zunächst möglich; bei Performance-/Dateisystemproblemen ein getrenntes Linux-Buildverzeichnis verwenden und dessen Quell-SHA dokumentieren.

Im Implementierungs-Worktree unter WSL:

```bash
cd /mnt/c/Users/finnn/Documents/GitHub/specter-diy/.codex-worktrees/esp32p4-ui-fixes
git status --short
git branch --show-current
ports/esp32p4/tools/setup.sh
ports/esp32p4/tools/build.sh
```

`setup.sh` installiert seine Abhängigkeiten in `ports/esp32p4/deps/` und initialisiert relevante Submodule. Den Bootloader-Abhängigkeitscommit ebenfalls dokumentieren: dessen Script folgt einem Branch. Die benötigten Host-Pakete anhand von IDF-Setup-Fehlern ergänzen. Nicht auf IDF 6 oder neues MicroPython/LVGL aktualisieren, um diese Fehler zu lösen. Die aktuelle Online-IDF-Dokumentation kann andere APIs zeigen; maßgeblich sind die lokalen 5.5.5-Header und die konkret gepinnte LVGL-Version.

## 6. Umsetzung in überprüfbaren Schritten

### A. Baseline und Diagnose

1. Worktree/Branch/Basis prüfen, lokale Regeln lesen, Submodul-SHAs erfassen. Baseline bauen, bevor Fixes beginnen.
2. Mit Test-PIN auf dem Board beide Fehler reproduzieren und filmen, möglichst 60/120 fps. Pressed-Feedback von Tearing, Backlight-Flicker und Touch-Flattern unterscheiden.
3. Temporär vor/nach `update_layout()` Label- und Dialogmaße messen. Keinen PIN loggen.
4. Flush-Rechtecke, Dauer, Transferabschluss und Display-Ereignisse mit begrenzten Debug-Zählern messen. Keine Ausgabe je Pixel/Zeile und kein dauerhaftes Log-Spamming.

### B. Dialog separat reparieren

1. In `Modal.set_text()` Text setzen, Label-Breite festlegen, Layout aktualisieren und erst dann Maße verwenden; alternativ LVGL-Content-Sizing mit nachgewiesen korrekter Eltern-/Kindbeziehung einsetzen.
2. Dialog mindestens so hoch wie Text plus vertikales Padding; auf verfügbare Displayhöhe begrenzen, ausreichend Breite und Ränder erhalten. Bestehenden Look mit dunklem Vollflächen-Backdrop, opakem Dialog und lesbarer Textfarbe erhalten.
3. Nach Größenänderung Label und Dialog passend zur bisherigen Anzeige ausrichten. Legacy-Leerzeilen nur entfernen, wenn echtes Padding dieselbe beabsichtigte Darstellung liefert.
4. Mehrzeiligen Text und wiederholten Wechsel kurz/lang testen. Loader muss vor Beginn der blockierenden PIN-Verifikation tatsächlich präsentiert sein. Zwei `update()`-Aufrufe nicht ohne gleichwertige Present-/Completion-Garantie entfernen.
5. Loader erstellen/aktualisieren/löschen mehrfach prüfen; kein Stapeln, Scrollen, abgeschnittener Text oder Tap durch das Modal.

### C. Rendering nach Marcos Prinzip reparieren

1. In den IDF-5.5.5-Quellen exakt klären, wie DPI zwei Framebuffer, Framewechsel und `on_color_trans_done`/`on_refresh_done` unterstützen. Ein VSYNC-Ereignis allein beweist nicht, dass ein alter Scanout-Puffer bereits frei ist. ISR-Kontext und erlaubte LVGL-Aufrufe für diesen Build prüfen.
2. Zunächst **zwei RGB565-Framebuffers** und `FULL`-Rendering als überschaubare korrekte Baseline verwenden, sofern IDF/LVGL dies unterstützen: vollständiges Bild in freien Backbuffer rendern, Cache für DMA sichtbar machen, vollständig fertiges Bild am bestätigten Frameübergang präsentieren, vorherigen Frontbuffer erst danach freigeben.
3. Speicherkosten bei 480×800: 768.000 Bytes je RGB565-Frame; zwei = 1.536.000 Bytes. PSRAM-DMA-Alignment, Cache-Sync und RAM unter Kamera-/QR-Last mitprüfen. Ein dritter Puffer nur bei nachgewiesenem Bedarf, nicht allein wegen Marcos Animationen.
4. Flush-State-Machine zentral im BSP/LVGL-HAL halten. Keine Python-Callbacks/Heap-Allokationen aus ISR. Pro akzeptiertem Flush genau eine Freigabe zum versionsgemäß sicheren Zeitpunkt; Fehler/Timeouts dürfen weder doppelt freigeben noch Deadlock verursachen.
5. `p4board_framebuffer()` und direkte `framebuf`-Nutzer mitprüfen: bisher geben sie den sichtbaren Puffer zurück. API und Besitzer nach dem Umbau eindeutig definieren; Demo/Kamera dürfen keine unbekannten Scanout-Puffer überschreiben.
6. Farbformat RGB565 erhalten. Asynchrone DMA-Rechteckkopien nur dort hinzufügen, wo tatsächlich eine Kopie nötig ist; das Aktivieren eines DMA-Flags allein löst die Single-Buffer-Race nicht.
7. Falls Full-Frame-Bandbreite nicht genügt, anschließend `DIRECT` mit zwei Puffern oder Partial-Rendering optimieren. Dabei beide Bilder mit unveränderten Bereichen konsistent halten; keine veralteten Rechtecke nach A/B-Wechseln. Das ist eine zweite Optimierung nach sichtbarer Korrektheit.
8. Simulator und F469-Pfade erhalten. Capability-Probes für neue optionale Funktionen nutzen. Weder F469-DMA2D-IRQs noch Marcos SDRAM-Adressen übernehmen. Kein Flackern durch längere Sleeps, abgeschaltetes Button-Feedback oder Display-Aus/Ein kaschieren.

### D. Verifikation und Übergabe

- Native Suite gemäß Port-Checkout ausführen; bestehende Fehler gegen unveränderte Basis abgleichen. Im bisherigen Root-Checkout nutzt CI `cd test && python3 run_native_tests.py`; genaue Verfügbarkeit im Port prüfen.
- Kleine gezielte Layout-Regression: erstmals langer Text sowie kurz→lang→kurz. Echte LVGL-Maße prüfen; reine Mocks können verzögertes Layout nicht beweisen.
- Hardware-Smoke: Boot/Next; gültige PIN; Fehlerdialog bei ungültiger Test-PIN mit Beachtung des Versuchslimits; Menüs, Back, OK, wiederholtes Loader-Öffnen, QR-Anzeige und Kamera sofern verfügbar.
- Mindestens 50 wiederholte Taps auf ungefährliche Menüs: genau eine Aktion pro Tap, keine Streifen/Leerbilder, Touch bleibt reaktionsfähig. Gehaltenen Button und Loslassen außerhalb mitprüfen.
- Mindestens 10 Minuten wechselnde Menüs/Loader/QR: kein Freeze, Watchdog, wachsender Speicherverbrauch oder Pufferfehler.
- Vorher/Nachher-Video mit gleicher Kameraeinstellung vergleichen. Ziel: kein reproduzierbares sichtbares Flackern; Render-/Present-Dauer im Verhältnis zum gemessenen Display-Frameintervall dokumentieren, keine unbelegte FPS-Zahl behaupten.
- Zwei unabhängige lokale Commits für Dialog und Treiber erleichtern Review/Rollback. Nur eigene Änderungen stagen. Dokumentation um tatsächliche Resultate, offene Grenzen und finale Hashes ergänzen.

## 7. Flashen und Kern-Bundle

[Kern Web Flasher](https://odudex.github.io/Kern/flash/) ist der Transport für das ZIP. Die Fehlerbehebung gehört in die Firmware, nicht in den Flasher. Keine Änderung am Kern-Projekt nötig.

Altes Bundle:

| Adresse | Datei | Bedeutung |
|---|---|---|
| `0x2000` | `bootloader.bin` | ESP32-P4-Bootloader |
| `0x8000` | `partition-table.bin` | Partitionstabelle |
| `0x10000` | `specter-diy.bin` | App, Build-Output heißt `micropython.bin` |
| `0x410000` | `storage-reset.bin` | setzt internen Speicher zurück |

Für einen UI-Fix Partitionen unverändert lassen und zunächst nur das App-Image flashen. Neue Offsets immer mit Build-`flash_args` und Partitionstabelle abgleichen. Windows-Beispiel nach Bereitstellung des neuen Images, nicht während der Planung ausführen:

```powershell
python -m esptool --chip esp32p4 -p COM5 -b 460800 write-flash --flash-mode dio --flash-size 16MB 0x10000 C:\path\to\specter-diy.bin
```

Esptool-Version und Syntax vorab prüfen. Das Port-Script schreibt Bootloader, Partitionstabelle und App, aber keinen Storage-Reset. Die vier Dateien des alten ZIP dagegen löschen beim vollständigen Flash bestehende Daten. Für die Fix-Übergabe ein **Update-ZIP ohne `storage-reset.bin`** und ohne Reset-Eintrag in `flasher_args.json` erzeugen, dessen Verhalten im Kern-Flasher prüfen und im README klar benennen. Falls ein separates Clean-Install-Bundle benötigt wird, ausdrücklich als löschend kennzeichnen. Unveränderte getestete Bootloader-/Partition-Images können im Paket bleiben, wenn die Hashes/Layout-Kompatibilität geprüft sind.

Paket enthält passende `flasher_args.json`, `SHA256SUMS.txt`, Firmware-Version/Commit und Board-/Chiprevision. SHA-Prüfung bestätigt Transportintegrität; der Beleg für die Fehlerbehebung ist der Hardware-Test.

## 8. Startprompt für Luna

> Arbeite im Worktree `C:\Users\finnn\Documents\GitHub\specter-diy\.codex-worktrees\esp32p4-ui-fixes` auf `fix/esp32p4-modal-button-flicker`. Lies das `AGENTS.md` im Haupt-Repository und `docs/ESP32P4_UI_FIX_PLAN.md`. Implementiere beide UI-Fixes auf Basis `870767d0e6a807fec5f7a1b473fe9319e273d0d7`, zuerst Dialog, dann Display-Puffer/Synchronisation nach Marcos Render-Prinzip. Bestätige Hypothesen durch Layout-/Flush-Messungen. Baue mit dem bestehenden ESP32-P4-Setup und liefere ein geprüftes Update-ZIP samt Hardware-Testnachweis, soweit Boardzugriff möglich ist. Bewahre die fremden Änderungen im Haupt-Checkout. Keine Veröffentlichung ohne unmittelbar vorher eingeholte Bestätigung. Kein Storage-Reset, keine Partition-/eFuse-Änderung für diese UI-Fixes. Dokumentiere fehlende Hardware-Abnahme ehrlich.
