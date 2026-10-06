#!/usr/bin/env bash
# Runtime GO判定の実行Wrapper（設計書 v1.8 §26.3、§28）。
#
# 使い方:
#   ./run_verification.sh <RELEASE_SCOPE> [MANIFEST] [EVIDENCE_ROOT] [REPORT_DIR]
#
# 例:
#   ./run_verification.sh MVP0-A
#   ./run_verification.sh MVP0-A ../runtime-evidence/r-001/manifest.json \
#                                ../runtime-evidence/r-001 ../release
#
# v1.6からの変更:
#   - RELEASE_SCOPE が必須。Scopeなしの判定を許さない
#   - Registry Snapshot の再生成と一致確認を判定前に行う
#   - Verifier自己試験（AT-VERIFIER-*）を判定前に実行する
#   - Reportの出力先を evidence-root の外へ変更した
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# 正規の実行環境はWSL2であり python3 が既定。検証用に上書きできる
PYTHON="${PYTHON:-python3}"
# ZIP展開時の文字コード差異を回避するため、機械処理はASCII固定名の正本を使用する。
DESIGN="${DESIGN_PATH:-$ROOT/design-v1.24-runtime-go.md}"
if [ ! -f "$DESIGN" ]; then
  echo "design file missing: $DESIGN" >&2
  exit 4
fi
REGISTRIES="$ROOT/design-source/registries"
SNAPSHOT="$ROOT/registry-snapshot.json"
VERIFIER_INTEGRITY="$ROOT/ci/verifier-source.sha256"

if [ $# -lt 1 ]; then
  echo "usage: $0 <RELEASE_SCOPE> [MANIFEST] [EVIDENCE_ROOT] [REPORT_DIR]" >&2
  echo "  RELEASE_SCOPE: MVP0-A | MVP0-B | MVP1-A | MVP0-C | MVP1-D" >&2
  exit 4
fi

SCOPE="$1"
MANIFEST="${2:-$ROOT/runtime-go-manifest.$SCOPE.template.json}"
EVIDENCE_ROOT="${3:-$ROOT/evidence}"
REPORT_DIR="${4:-$ROOT/release}"

echo "=== [1/4] Registry Snapshot の鮮度確認 ==="
INTEGRITY_ARGS=(--target "$ROOT/verify_runtime_go.py" --expected "$VERIFIER_INTEGRITY")
if [ -n "${RUNTIME_GO_VERIFIER_SHA256:-}" ]; then
  INTEGRITY_ARGS+=(--trusted-hash "$RUNTIME_GO_VERIFIER_SHA256")
fi
"$PYTHON" "$ROOT/tools/check_verifier_integrity.py" "${INTEGRITY_ARGS[@]}"
TMP_SNAPSHOT="$(mktemp -t snapshot.XXXXXX.json)"
trap 'rm -f "$TMP_SNAPSHOT"' EXIT
"$PYTHON" "$ROOT/tools/build_registry_snapshot.py" \
  --registries "$REGISTRIES" --design "$DESIGN" --out "$TMP_SNAPSHOT" >/dev/null
if ! "$PYTHON" - "$SNAPSHOT" "$TMP_SNAPSHOT" <<'PY'; then
import json, sys
a = json.load(open(sys.argv[1], encoding='utf-8'))
b = json.load(open(sys.argv[2], encoding='utf-8'))
if a.get('registry_snapshot_hash') != b.get('registry_snapshot_hash'):
    print('registry-snapshot.json is stale. run tools/build_registry_snapshot.py',
          file=sys.stderr)
    raise SystemExit(1)
print(f"  snapshot ok: {a['registry_snapshot_hash']}")
PY
  exit 3
fi

echo "=== [2/4] Spec Lint / Manifest Validator ==="
"$PYTHON" "$ROOT/tools/lint_spec.py" --design "$DESIGN" --registries "$REGISTRIES" \
  --snapshot "$SNAPSHOT" --spec-dir "$ROOT/spec" --release-scope "$SCOPE"
"$PYTHON" "$ROOT/tools/validate_test_manifest.py" --registries "$REGISTRIES" \
  --release-scope "$SCOPE"

echo "=== [3/4] Verifier自己試験（AT-VERIFIER-*）==="
# 判定器が壊れていれば、後段のEvidenceは全て意味を持たない
"$PYTHON" "$ROOT/tests/test_verify_runtime_go.py" 2>&1 | tail -3

echo "=== [4/4] Runtime GO判定 scope=$SCOPE ==="
mkdir -p "$REPORT_DIR"
set +e
"$PYTHON" "$ROOT/verify_runtime_go.py" \
  --design "$DESIGN" \
  --registry "$SNAPSHOT" \
  --release-scope "$SCOPE" \
  --manifest "$MANIFEST" \
  --evidence-root "$EVIDENCE_ROOT" \
  --emit-report "$REPORT_DIR/runtime-go-verification-report.json" \
  --emit-release-manifest "$REPORT_DIR/runtime-go-release-manifest.json"
CODE=$?
set -e

echo ""
case "$CODE" in
  0) echo "RUNTIME_GO ($SCOPE)。Release Manifestを生成できる" ;;
  2) echo "BLOCKED_EVIDENCE_MISSING。Evidence不足（正常な未達状態）" ;;
  3) echo "RUNTIME_NO_GO。不一致・偽装・件数不整合を検出" ;;
  4) echo "INPUT_INVALID。Manifest／Registry／Scope指定が不正" ;;
esac
exit "$CODE"
