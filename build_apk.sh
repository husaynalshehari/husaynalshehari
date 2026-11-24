#!/usr/bin/env bash
# Builds GoldExtractor.apk on an Ubuntu/Debian Linux server (run as root).
# Usage:  bash build_apk.sh
set -e
cd "$(dirname "$0")"

if [ "$(uname -m)" != "x86_64" ]; then
  echo "!! This server is $(uname -m). Android build tools only run on x86_64 Linux."
  echo "!! Use the GitHub Actions option or Android Studio on a PC instead."
  exit 1
fi

echo "==> [1/4] Installing Java 17..."
java_ok() { command -v javac >/dev/null 2>&1 && javac -version 2>&1 | grep -qE 'javac (1[7-9]|2[0-9])'; }
if ! java_ok; then
  apt-get update -y
  apt-get install -y openjdk-17-jdk-headless
  for d in /usr/lib/jvm/java-17-openjdk-*; do
    if [ -x "$d/bin/javac" ]; then export JAVA_HOME="$d"; export PATH="$d/bin:$PATH"; break; fi
  done
fi
command -v unzip >/dev/null 2>&1 || apt-get install -y unzip
command -v wget  >/dev/null 2>&1 || apt-get install -y wget

echo "==> [2/4] Installing Android SDK (first time only, ~600MB)..."
export ANDROID_HOME="$HOME/android-sdk"
SDKMANAGER="$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager"
if [ ! -x "$SDKMANAGER" ]; then
  mkdir -p "$ANDROID_HOME/cmdline-tools"
  wget -q --show-progress -O /tmp/cmdline-tools.zip \
    https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
  rm -rf "$ANDROID_HOME/cmdline-tools/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest"
  unzip -q /tmp/cmdline-tools.zip -d "$ANDROID_HOME/cmdline-tools"
  mv "$ANDROID_HOME/cmdline-tools/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest"
fi
yes | "$SDKMANAGER" --licenses >/dev/null 2>&1 || true
"$SDKMANAGER" "platforms;android-34" "build-tools;34.0.0" "platform-tools" >/dev/null

echo "sdk.dir=$ANDROID_HOME" > local.properties

echo "==> [3/4] Building the APK (first build downloads Gradle, a few minutes)..."
chmod +x gradlew
./gradlew assembleDebug --no-daemon

echo "==> [4/4] Done."
cp app/build/outputs/apk/debug/app-debug.apk ./GoldExtractor.apk
echo ""
echo "APK ready: $(pwd)/GoldExtractor.apk"
echo ""
echo "To download it to your phone, run this, then open http://YOUR_SERVER_IP:8000/GoldExtractor.apk"
echo "in the phone's browser (press Ctrl+C after the download to stop sharing):"
echo "    python3 -m http.server 8000"
