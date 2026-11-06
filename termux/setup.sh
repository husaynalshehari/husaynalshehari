#!/data/data/com.termux/files/usr/bin/bash
# gg.py on an Android phone, in Termux: installs what it needs, then gg.py and content.py in ~/gg,
# a «gg» command that starts it and a «gg-update» command that updates it. Run it again any time:
# it keeps ~/gg/data (accounts, tasks, saved tweets).
#
#   curl -fsSL -o setup.sh https://raw.githubusercontent.com/husaynalshehari/husaynalshehari/ccr-695f6c04-mlgxcr/termux/setup.sh && bash setup.sh
set -euo pipefail

RAW="https://raw.githubusercontent.com/husaynalshehari/husaynalshehari/ccr-695f6c04-mlgxcr"
GG_HOME="${GG_HOME:-$HOME/gg}"
step() { printf '\n\033[1;32m== %s\033[0m\n' "$*"; }
pip_install() {   # (if this Python refuses installing beside its own packages, it is told it may)
  python -m pip install --disable-pip-version-check "$@" || python -m pip install --disable-pip-version-check --break-system-packages "$@"
}
fail() { printf '\n\033[1;31m%s\033[0m\n' "$*"; exit 1; }

case "${PREFIX:-}" in
  */com.termux/*) ;;
  *) fail "هذا الملف لتطبيق Termux على أندرويد: افتح Termux وشغّله فيه." ;;
esac

step "1/6 تحديث Termux"
pkg update -y
pkg upgrade -y -o Dpkg::Options::=--force-confold

step "2/6 Python وNode وChromium (قد يأخذ عدة دقائق)"
pkg install -y x11-repo
pkg install -y python python-pip python-greenlet nodejs chromium curl

step "3/6 Flask"
pip_install flask pyee typing-extensions

step "4/6 Playwright"
# PyPI has no Playwright for Android, but the one for Linux on ARM runs there: its Python part is the same,
# and its only program built for Linux (its own Node) is not used: gg.py runs Playwright's driver on
# Termux's Node and opens Termux's Chromium.
work="$(mktemp -d)"
python -m pip download --disable-pip-version-check --no-deps --only-binary=:all: \
  --platform manylinux_2_17_aarch64 -d "$work" "playwright${PLAYWRIGHT_VERSION:+==$PLAYWRIGHT_VERSION}"
wheel="$(ls "$work"/playwright-*.whl)"
version="$(basename "$wheel" | cut -d- -f2)"
mv "$wheel" "$work/playwright-$version-py3-none-any.whl"
pip_install --no-deps --force-reinstall "$work/playwright-$version-py3-none-any.whl"
rm -rf "$work"
driver="$(python -c 'import os, playwright; print(os.path.join(os.path.dirname(playwright.__file__), "driver"))')"
ln -sf "$PREFIX/bin/node" "$driver/node"          # instead of the desktop one (and 100 MB less)

step "5/6 السكربت"
mkdir -p "$GG_HOME"
curl -fsSL -o "$GG_HOME/gg.py.new" "$RAW/script_original.py" && mv "$GG_HOME/gg.py.new" "$GG_HOME/gg.py"
curl -fsSL -o "$GG_HOME/content.py.new" "$RAW/content.py" && mv "$GG_HOME/content.py.new" "$GG_HOME/content.py"
cat > "$PREFIX/bin/gg" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# Start gg.py; its page is only for this phone (127.0.0.1). Stop it with Ctrl+C.
cd "$GG_HOME" || exit 1
termux-wake-lock 2>/dev/null || true
export BIND_HOST="\${BIND_HOST:-127.0.0.1}"
echo "افتح في متصفح الجوال: http://127.0.0.1:\${PORT:-5000}/\${X_ACCESS_PATH:-desk}"
exec python gg.py
EOF
cat > "$PREFIX/bin/gg-update" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# Update gg.py and content.py (then start gg again).
set -e
cd "$GG_HOME"
curl -fsSL -o gg.py.new "$RAW/script_original.py" && mv gg.py.new gg.py
curl -fsSL -o content.py.new "$RAW/content.py" && mv content.py.new content.py
echo "تم التحديث. شغّل: gg"
EOF
chmod +x "$PREFIX/bin/gg" "$PREFIX/bin/gg-update"
mkdir -p "$HOME/.shortcuts"                          # for the Termux:Widget app: «gg» on the home screen
printf '#!/data/data/com.termux/files/usr/bin/bash\nexec gg\n' > "$HOME/.shortcuts/gg"
chmod +x "$HOME/.shortcuts/gg"

step "6/6 تجربة المتصفح"
PLAYWRIGHT_NODEJS_PATH="$PREFIX/bin/node" python - <<'EOF'
import os, sys, tempfile
from playwright.sync_api import sync_playwright
prefix = os.environ["PREFIX"]
chrome = os.path.join(prefix, "lib", "chromium", "chrome")
if not os.path.isfile(chrome):
    chrome = os.path.join(prefix, "bin", "chromium-browser")
try:
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            tempfile.mkdtemp(), headless=True, executable_path=chrome,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"] + os.environ.get("GG_CHROMIUM_ARGS", "").split())
        page = context.pages[0] if context.pages else context.new_page()
        page.set_content("<p id=x>ok</p>")
        agent = page.evaluate("navigator.userAgent")
        print("المتصفح يعمل:", page.inner_text("#x"), "·", agent.split("Chrome/")[-1].split(" ")[0] if "Chrome/" in agent else agent)
        try:
            page.goto("https://example.com/", timeout=30000)
            print("والإنترنت من داخله يعمل:", page.title())
        except Exception as exc:
            print("المتصفح يعمل لكن تعذّر فتح موقع منه:", str(exc).splitlines()[0][:200])
        context.close()
except Exception as exc:
    print("تعذّر تشغيل المتصفح:", type(exc).__name__, str(exc).splitlines()[0][:300] if str(exc) else "")
    print("أرسل هذه الرسالة كما هي.")
    sys.exit(1)
EOF

step "تم"
printf 'الجهاز: %s · أندرويد %s · الذاكرة %s ميجا · المساحة الفارغة %s\n' \
  "$(getprop ro.product.model 2>/dev/null || echo ?)" "$(getprop ro.build.version.release 2>/dev/null || echo ?)" \
  "$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo ?)" "$(df -h "$HOME" | awk 'NR==2 {print $4}')"
echo "للتشغيل اكتب: gg   ثم افتح في متصفح الجوال: http://127.0.0.1:5000/desk"
echo "للتحديث لاحقًا: gg-update"
