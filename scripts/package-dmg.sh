#!/bin/bash
set -euo pipefail

PRODUCT_NAME="QuotaBar"
SCHEME_NAME="quota-bar"
PROJECT_FILE="quota-bar.xcodeproj"
# project.yml 在仓库根目录(本脚本按仓库根为工作目录运行)
PROJECT_YML="project.yml"

# 版本号从 project.yml 读单一事实源(手写副本会和 project.yml 漂移)。
# 顺带剥掉 YAML 值上可能的引号与行尾注释;BSD grep 不支持 \s,用 POSIX 字符类。
if [ ! -f "${PROJECT_YML}" ]; then
  echo "Error: ${PROJECT_YML} not found — run this script from the repository root." >&2
  exit 1
fi
# || true:键缺失时 grep 退出码 1,配合先前的 pipefail 会让 set -e 直接终止,
# 下面的"友好报错"永远打不出来
MARKETING_VERSION=$(grep -E '^[[:space:]]*MARKETING_VERSION:' "${PROJECT_YML}" | head -1 \
  | sed 's/.*MARKETING_VERSION:[[:space:]]*//' | sed 's/[[:space:]]*#.*$//' | tr -d '"'"'"' ' || true)
if ! printf '%s' "${MARKETING_VERSION}" | grep -Eq '^[0-9]+(\.[0-9]+)*$'; then
  echo "Error: invalid MARKETING_VERSION '${MARKETING_VERSION}' in ${PROJECT_YML}" >&2
  exit 1
fi

# 工具存在性前置检查:缺失时报人话,而不是让底层命令报一堆费解的错误
for tool in xcodebuild create-dmg; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "Error: required tool '${tool}' not found in PATH." >&2
    if [ "${tool}" = "create-dmg" ]; then
      echo "Install it with: brew install create-dmg" >&2
    fi
    exit 1
  fi
done

# 1. Build
echo "==> Building ${PRODUCT_NAME}..."
xcodebuild -project "${PROJECT_FILE}" \
  -scheme "${SCHEME_NAME}" \
  -configuration Release \
  -quiet \
  build

# 2. Locate build output
BUILD_DIR=$(xcodebuild -project "${PROJECT_FILE}" -scheme "${SCHEME_NAME}" -configuration Release -showBuildSettings 2>/dev/null | grep "BUILT_PRODUCTS_DIR = " | head -1 | sed 's/.*= //')
APP_PATH="${BUILD_DIR}/${PRODUCT_NAME}.app"

if [ ! -d "${APP_PATH}" ]; then
  echo "Error: App not found at ${APP_PATH}"
  exit 1
fi

# 3. Create dist directory
mkdir -p dist

# 4. Package DMG with create-dmg
echo "==> Creating DMG..."
create-dmg \
  --volname "${PRODUCT_NAME}" \
  --window-pos 200 200 \
  --window-size 600 400 \
  --hide-extension "${PRODUCT_NAME}.app" \
  --app-drop-link 480 170 \
  --icon-size 100 \
  --icon "${PRODUCT_NAME}.app" 150 170 \
  "dist/${PRODUCT_NAME}-${MARKETING_VERSION}.dmg" \
  "${APP_PATH}"

echo "==> DMG created at dist/${PRODUCT_NAME}-${MARKETING_VERSION}.dmg"
