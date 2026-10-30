#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$ROOT/scripts/update-appcast.sh"
FIX="$ROOT/scripts/tests/fixtures/appcast-sample.xml"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for cand in python python3; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >/dev/null 2>&1; then
      PYTHON="$cand"; break
    fi
  done
fi
[ -n "$PYTHON" ] || { echo "FAIL: no usable python" >&2; exit 1; }
fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

# tiny fake dmg
dd if=/dev/zero of="$TMP/fake.dmg" bs=1024 count=2 2>/dev/null
SIZE=$(wc -c < "$TMP/fake.dmg" | tr -d " ")

# 断言 XML 里 sparkle:version=<build> 的 enclosure 各属性值正确。
# 只验"XML 能解析"是不够的:URL 被写成空串、版本没替换成功、length 错,
# XML 依然良构 —— 发布链最不该静默的地方必须有值断言。
check_enclosure() {
  local file="$1" build="$2" url="$3" short="$4" length="$5"
  "$PYTHON" - "$file" "$build" "$url" "$short" "$length" <<'PY' || fail "enclosure values wrong in $file"
import sys, xml.etree.ElementTree as ET
path, build, want_url, want_short, want_len = sys.argv[1:6]
SP = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
root = ET.parse(path).getroot()
for enc in root.iter("enclosure"):
    if enc.get(SP + "version") == build:
        assert enc.get("url") == want_url, f"url {enc.get('url')!r} != {want_url!r}"
        assert enc.get(SP + "shortVersionString") == want_short, f"short {enc.get(SP + 'shortVersionString')!r} != {want_short!r}"
        assert enc.get("length") == want_len, f"length {enc.get('length')!r} != {want_len!r}"
        break
else:
    raise AssertionError(f"no enclosure with sparkle:version={build}")
PY
}

# --- insert succeeds ---
cp "$FIX" "$TMP/a.xml"
"$SCRIPT" --marketing 2.0.4 --build 5 --dmg "$TMP/fake.dmg" --appcast "$TMP/a.xml" \
  --date "Thu, 16 Jul 2026 10:58:00 +0800" || fail "insert should succeed"
grep -q 'sparkle:version="5"' "$TMP/a.xml" || fail "missing version 5"
grep -q 'sparkle:shortVersionString="2.0.4"' "$TMP/a.xml" || fail "missing shortVersionString"
grep -q "length=\"$SIZE\"" "$TMP/a.xml" || fail "bad length"
check_enclosure "$TMP/a.xml" 5 \
  "https://github.com/nmsn/quota-bar/releases/download/v2.0.4/QuotaBar-2.0.4.dmg" "2.0.4" "$SIZE" \
  || fail "insert enclosure values"
# new item must appear before 2.0.3
"$PYTHON" - <<'PY' "$TMP/a.xml" || fail "order"
import sys
t=open(sys.argv[1]).read()
i5=t.find('sparkle:version="5"')
i3=t.find('sparkle:version="2.0.3"')
assert i5!=-1 and i3!=-1 and i5<i3
PY
pass "insert"

# --- insert into empty appcast (no items yet) ---
cat > "$TMP/empty.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>QuotaBar Updates</title>
  </channel>
</rss>
EOF
"$SCRIPT" --marketing 1.0.0 --build 1 --dmg "$TMP/fake.dmg" --appcast "$TMP/empty.xml" \
  || fail "insert into empty appcast should succeed"
check_enclosure "$TMP/empty.xml" 1 \
  "https://github.com/nmsn/quota-bar/releases/download/v1.0.0/QuotaBar-1.0.0.dmg" "1.0.0" "$SIZE" \
  || fail "empty appcast insert values"
pass "insert into empty appcast"

# --- duplicate build fails ---
cp "$TMP/a.xml" "$TMP/b.xml"
if "$SCRIPT" --marketing 2.0.5 --build 5 --dmg "$TMP/fake.dmg" --appcast "$TMP/b.xml"; then
  fail "duplicate build should fail"
fi
pass "duplicate build"

# --- build not greater than existing integer max (after we have integer 5) ---
if "$SCRIPT" --marketing 2.0.5 --build 4 --dmg "$TMP/fake.dmg" --appcast "$TMP/b.xml"; then
  fail "build 4 <= 5 should fail"
fi
pass "monotonic"

# --- missing dmg fails ---
cp "$FIX" "$TMP/c.xml"
if "$SCRIPT" --marketing 2.0.4 --build 5 --dmg "$TMP/nope.dmg" --appcast "$TMP/c.xml"; then
  fail "missing dmg should fail"
fi
pass "missing dmg"

# --- replace enclosure version ---
cat > "$TMP/d.xml" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>QuotaBar Updates</title>
    <link>https://nmsn.github.io/quota-bar/appcast.xml</link>
    <item>
      <title>Version 2.0.4</title>
      <pubDate>Thu, 16 Jul 2026 10:58:00 +0800</pubDate>
      <enclosure url="https://github.com/nmsn/quota-bar/releases/download/v2.0.4/QuotaBar-2.0.4.dmg" sparkle:version="2.0.4" length="1" type="application/octet-stream"/>
    </item>
  </channel>
</rss>
EOF
"$SCRIPT" --marketing 2.0.4 --build 5 --dmg "$TMP/fake.dmg" --appcast "$TMP/d.xml" \
  --replace-enclosure-version 2.0.4 || fail "replace should succeed"
grep -q 'sparkle:version="5"' "$TMP/d.xml" || fail "replace version"
grep -q 'sparkle:shortVersionString="2.0.4"' "$TMP/d.xml" || fail "replace short"
grep -q "length=\"$SIZE\"" "$TMP/d.xml" || fail "replace length"
check_enclosure "$TMP/d.xml" 5 \
  "https://github.com/nmsn/quota-bar/releases/download/v2.0.4/QuotaBar-2.0.4.dmg" "2.0.4" "$SIZE" \
  || fail "replace enclosure values"
if grep -q 'sparkle:version="2.0.4"' "$TMP/d.xml"; then fail "old version should be gone"; fi
pass "replace"

# --- replace with single-quoted url attribute: must still actually update ---
cat > "$TMP/sq.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <item>
      <title>Version 2.0.8</title>
      <enclosure url='https://old.example.com/a.dmg' sparkle:version="8" length="1" type="application/octet-stream"/>
    </item>
  </channel>
</rss>
EOF
"$SCRIPT" --marketing 2.0.9 --build 9 --dmg "$TMP/fake.dmg" --appcast "$TMP/sq.xml" \
  --replace-enclosure-version 8 --url 'https://example.com/new.dmg' \
  || fail "replace with single-quoted url should succeed"
check_enclosure "$TMP/sq.xml" 9 "https://example.com/new.dmg" "2.0.9" "$SIZE" \
  || fail "single-quoted url was not replaced"
pass "replace single-quoted url"

# --- replace missing OLD fails ---
cp "$FIX" "$TMP/e.xml"
if "$SCRIPT" --marketing 2.0.4 --build 5 --dmg "$TMP/fake.dmg" --appcast "$TMP/e.xml" \
  --replace-enclosure-version 9.9.9; then
  fail "replace missing should fail"
fi
pass "replace missing"

# --- XML escaping: special chars in title/url must stay well-formed AND correct ---
cp "$FIX" "$TMP/f.xml"
"$SCRIPT" --marketing 2.0.4 --build 5 --dmg "$TMP/fake.dmg" --appcast "$TMP/f.xml" \
  --title 'A & B <C> "D"' --url 'https://example.com/dl?a=1&b=2' || fail "escape insert should succeed"
check_enclosure "$TMP/f.xml" 5 'https://example.com/dl?a=1&b=2' "2.0.4" "$SIZE" \
  || fail "escaped url/short values must round-trip"
"$PYTHON" - "$TMP/f.xml" <<'PY' || fail "insert with & / < / quotes must produce well-formed XML"
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
titles = [e.text for e in root.iter("title")]
assert 'A & B <C> "D"' in titles, titles
PY
pass "insert XML escaping"

# replace path: same escaping requirement (marketing also contains '&')
cat > "$TMP/g.xml" <<'XEOF'
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>QuotaBar Updates</title>
    <item>
      <title>Version 2.0.6</title>
      <enclosure url="https://old.example.com/a.dmg" sparkle:version="6" length="1" type="application/octet-stream"/>
    </item>
  </channel>
</rss>
XEOF
"$SCRIPT" --marketing '2.0&7' --build 7 --dmg "$TMP/fake.dmg" --appcast "$TMP/g.xml" \
  --replace-enclosure-version 6 --url 'https://example.com/dl?a=1&b=2' || fail "escape replace should succeed"
check_enclosure "$TMP/g.xml" 7 'https://example.com/dl?a=1&b=2' '2.0&7' "$SIZE" \
  || fail "replace escaping must round-trip"
pass "replace XML escaping"

echo "All script tests passed."
