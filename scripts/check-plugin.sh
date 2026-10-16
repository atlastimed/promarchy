#!/usr/bin/env bash
# Static checks for the Omarchy plugin contract (the same rules as
# `omarchy plugin validate`), manifest/settings consistency, and QML syntax.
# qmllint cannot resolve Omarchy's runtime-only qs.* modules offline, so only
# genuine syntax errors fail the build.
set -euo pipefail
cd "$(dirname "$0")/.."

fail() {
  echo "check-plugin: $*" >&2
  exit 1
}

manifest=manifest.json
jq -e . "$manifest" >/dev/null || fail "manifest.json is not valid JSON"
jq -e '.schemaVersion == 1' "$manifest" >/dev/null || fail "schemaVersion must be the number 1"
for field in id name version kinds entryPoints; do
  jq -e --arg f "$field" 'has($f)' "$manifest" >/dev/null || fail "manifest is missing '$field'"
done

id=$(jq -r .id "$manifest")
[[ $id =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ && $id != *..* && $id != omarchy.* ]] || fail "invalid plugin id '$id'"

while IFS= read -r entry; do
  [[ $entry != /* && $entry != *..* ]] || fail "entry point must be a safe relative path: $entry"
  [[ -f $entry ]] || fail "entry point not found: $entry"
done < <(jq -r '.entryPoints[]' "$manifest")

for pair in bar-widget:barWidget service:service; do
  kind=${pair%%:*} key=${pair##*:}
  if jq -e --arg k "$kind" '.kinds | index($k) != null' "$manifest" >/dev/null; then
    jq -e --arg e "$key" '.entryPoints | has($e)' "$manifest" >/dev/null || fail "kind '$kind' needs entryPoints.$key"
  fi
done

link=$(find . \( -name .git -o -name .venv -o -name node_modules \) -prune -o -type l -print -quit)
[[ -z $link ]] || fail "symlinks are not allowed in a plugin: $link"

jq -e '(.barWidget.defaults | keys | sort) == ([.barWidget.schema[].key] | sort)' "$manifest" >/dev/null \
  || fail "barWidget.defaults and barWidget.schema list different settings"
jq -e '.barWidget as $w | all($w.schema[]; .defaultValue == $w.defaults[.key])' "$manifest" >/dev/null \
  || fail "a schema defaultValue disagrees with barWidget.defaults"

version=$(jq -r .version "$manifest")
grep -q "^__version__ = \"$version\"" backend/promarchy/__init__.py || fail "backend version differs from manifest $version"
grep -q "^version = \"$version\"" pyproject.toml || fail "pyproject version differs from manifest $version"
grep -q "moduleName: \"$id\"" BarWidget.qml || fail "BarWidget moduleName must be $id"
grep -q "pluginId: \"$id\"" BarWidget.qml || fail "BarWidget pluginId must be $id"

qmllint=${QMLLINT:-$(command -v qmllint6 || command -v qmllint || echo /usr/lib/qt6/bin/qmllint)}
output=$("$qmllint" ./*.qml 2>&1 || true)
if grep -q '\[syntax\]' <<<"$output"; then
  grep -A2 '\[syntax\]' <<<"$output" >&2
  fail "QML syntax errors"
fi

python3 -I backend/promarchyctl --version >/dev/null || fail "backend does not start"
echo "check-plugin: $id $version ok"
