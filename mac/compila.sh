#!/bin/bash
# Costruisce Murnau.app completa: l'app nativa (mac/Murnau.swift, per Apple Silicon e Intel) con dentro tutto il
# programma (Contents/Resources/programma), firmata (firma locale, senza account Apple), in
# ~/Library/Caches/Murnau/Murnau.app: fuori dalla cartella del codice, che sta in Drive (lì Spotlight la troverebbe).
# Con --installa la mette nella cartella Applicazioni dell'utente (~/Applications), al posto di quella che c'è.
# Bastano gli strumenti da riga di comando di Xcode (xcode-select --install). Dalla cartella del codice di Murnau:
#     mac/compila.sh [--installa]
set -euo pipefail
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
for arch in arm64 x86_64; do
  swiftc -O -swift-version 5 -target "$arch-apple-macos13.0" -framework AppKit -framework WebKit \
    mac/Murnau.swift -o "$tmp/Murnau-$arch" 2> >(grep -v "libswiftCompatibilityPacks" >&2)
done

app="$tmp/Murnau.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/programma"
lipo -create -output "$app/Contents/MacOS/Murnau" "$tmp/Murnau-arm64" "$tmp/Murnau-x86_64"
cp mac/Info.plist "$app/Contents/Info.plist"
cp mac/murnau.icns "$app/Contents/Resources/murnau.icns"
# La versione mostrata dal Finder è quella del programma (VERSIONE).
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $(tr -d '[:space:]' < VERSIONE)" "$app/Contents/Info.plist"
# Il programma: i file che .gitignore ammette (mai film, montaggi o segnalazioni), tranne i sorgenti dell'app.
git ls-files -z --cached --others --exclude-standard | while IFS= read -r -d '' f; do
  case "$f" in .gitignore|mac/*) continue ;; esac
  [ -f "$f" ] || continue
  mkdir -p "$app/Contents/Resources/programma/$(dirname "$f")"
  cp -p "$f" "$app/Contents/Resources/programma/$f"
done
codesign --force --deep --sign - "$app"
codesign --verify --deep --strict "$app"

costruita="$HOME/Library/Caches/Murnau/Murnau.app"
mkdir -p "$(dirname "$costruita")"
rm -rf "$costruita"
mv "$app" "$costruita"
echo "Pronta $costruita, versione $(tr -d '[:space:]' < VERSIONE) ($(lipo -archs "$costruita/Contents/MacOS/Murnau"))."

if [ "${1:-}" = "--installa" ]; then
  destinazione="$HOME/Applications/Murnau.app"
  mkdir -p "$HOME/Applications"
  # Si prepara accanto e poi si scambia: un Murnau aperto continua a girare fino alla prossima apertura.
  rm -rf "$HOME/Applications/.Murnau.app.nuova" "$HOME/Applications/.Murnau.app.vecchia"
  ditto "$costruita" "$HOME/Applications/.Murnau.app.nuova"
  [ -e "$destinazione" ] && mv "$destinazione" "$HOME/Applications/.Murnau.app.vecchia"
  mv "$HOME/Applications/.Murnau.app.nuova" "$destinazione"
  rm -rf "$HOME/Applications/.Murnau.app.vecchia"
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$destinazione"
  echo "Installata in $destinazione."
fi
