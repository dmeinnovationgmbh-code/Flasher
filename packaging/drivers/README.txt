Tactrix / J2534 Treiber – hier ablegen
======================================

Lege in diesen Ordner den offiziellen Tactrix-Treiber (Openport 2.0 J2534)
ODER einen beliebigen J2534-Treiber-Installer, BEVOR du den Windows-Installer
baust (iscc packaging\windows_installer.iss).

Der App-Installer kopiert diesen Ordner beim Setup nach %TEMP%\drivers und
startet danach automatisch (still) jede hier gefundene .exe / .msi. So ist der
Treiber nach der App-Installation sofort da – ohne separaten Download.

Unterstuetzt:
  *.exe   -> wird mit Silent-Flag ausgefuehrt (Default: /S – passt fuer die
             NSIS-basierten Tactrix- und FTDI-CDM-Installer)
  *.msi   -> wird mit "msiexec /i <datei> /qn /norestart" ausgefuehrt

Anderer Silent-Schalter noetig? Lege eine Textdatei "_silent_args.txt" dazu;
ihre erste Zeile wird statt "/S" an jede .exe uebergeben
  z. B. InstallShield:  /s /v"/qn"
        Inno Setup:     /VERYSILENT /SUPPRESSMSGBOXES /NORESTART

Diese README wird nicht ausgefuehrt (kein .exe/.msi) und stoert nichts.
Der Treiber-Schritt erscheint im Installer nur, wenn hier wirklich ein
Installer liegt – siehe DriversPresent() in windows_installer.iss.

Bezugsquelle Tactrix-Treiber: https://www.tactrix.com/  (Download -> Openport 2.0)
