#!/usr/bin/env bash
# Builds «المستخرج» (extract.apk) with the Android SDK's own tools, without Gradle:
# aapt2 (resources and manifest) -> javac -> d8 (dex) -> zipalign -> apksigner.
# The APK is signed with signing.jks. The key is never put in the repository: keep your copy and put it back
# here before building, so that each new version installs over the last one (a new key means uninstalling first).
set -euo pipefail
cd "$(dirname "$0")"

SDK=${ANDROID_SDK_ROOT:-/opt/android-sdk}
TOOLS=$SDK/build-tools/35.0.0
ANDROID_JAR=$SDK/platforms/android-35/android.jar
OUT=build
KEY_PASS=${EXTRACT_KEY_PASS:?set EXTRACT_KEY_PASS to the password of signing.jks}

if [ ! -f signing.jks ]; then
    echo "signing.jks is missing: a new key is made (this APK will not install over one signed with another key)"
    keytool -genkeypair -keystore signing.jks -storetype PKCS12 -alias extract -keyalg RSA -keysize 2048 \
        -validity 10000 -storepass "$KEY_PASS" -keypass "$KEY_PASS" -dname "CN=Extract, O=Extract"
fi

rm -rf "$OUT"
mkdir -p "$OUT/gen" "$OUT/classes" "$OUT/dex" "$OUT/test"

# The rules that need no phone are checked first.
javac -encoding UTF-8 -d "$OUT/test" src/app/tg/extract/Text.java test/TextTest.java
java -cp "$OUT/test" app.tg.extract.TextTest

"$TOOLS/aapt2" compile --dir res -o "$OUT/res.zip"
"$TOOLS/aapt2" link -o "$OUT/base.apk" -I "$ANDROID_JAR" --manifest AndroidManifest.xml \
    --java "$OUT/gen" "$OUT/res.zip"

javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options -bootclasspath "$ANDROID_JAR" -classpath "$TOOLS/core-lambda-stubs.jar" \
    -d "$OUT/classes" $(find src "$OUT/gen" -name '*.java')
(cd "$OUT/classes" && jar cf ../classes.jar .)
"$TOOLS/d8" --release --min-api 26 --lib "$ANDROID_JAR" --output "$OUT/dex" "$OUT/classes.jar"

cp "$OUT/base.apk" "$OUT/unsigned.apk"
(cd "$OUT/dex" && zip -q -X ../unsigned.apk classes.dex)
"$TOOLS/zipalign" -p -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"
"$TOOLS/apksigner" sign --ks signing.jks --ks-pass env:EXTRACT_KEY_PASS --key-pass env:EXTRACT_KEY_PASS \
    --out "$OUT/extract.apk" "$OUT/aligned.apk"
"$TOOLS/apksigner" verify "$OUT/extract.apk"
echo "built: $OUT/extract.apk"
