#!/usr/bin/env bash
# ローカル検証の入口。**通常開発はここで確かめる。**
#
# GitHub Actions は「安全性に直結する最小 PR チェック」と「Release 時の完全検査」
# だけに絞ってある（docs/CI-POLICY.md）。日々の確認はこの Script が担う。
#
# 重要: ここの PASS は **GitHub CI の PASS ではない**。Release Evidence でもない。
# Runtime GO は WSL2 実機での Release Gate 実行だけが生成できる（不変条件#19）。
# この Script が出すのは「手元で壊していないこと」だけである。
#
#   ./run-checks.sh          全部
#   ./run-checks.sh fast     型・lint・test だけ（正本検査を飛ばす）
#
# 終了 Code
#   0 : 全部通った
#   1 : 1 件以上落ちた
set -u
cd "$(dirname "$0")" || exit 1

MODE="${1:-all}"
DESIGN="$(python tools/design_identity.py --print-path 2>/dev/null || echo '')"
if [ -z "$DESIGN" ]; then
  # design_identity が --print-path を持たない場合は実在する正本を拾う。
  DESIGN="$(ls -1 design-v*-runtime-go.md 2>/dev/null | tail -1)"
fi

failed=0
passed=0

run() {
  local label="$1"
  shift
  printf '%-34s ' "$label"
  if "$@" >/tmp/run-checks-last.log 2>&1; then
    echo "PASS"
    passed=$((passed + 1))
  else
    echo "FAIL"
    failed=$((failed + 1))
    sed -n '1,25p' /tmp/run-checks-last.log | sed 's/^/    /'
  fi
}

echo "== ローカル検証（$MODE）=="
echo "設計書: ${DESIGN:-（見つからない）}"
echo

if [ "$MODE" != "fast" ]; then
  # --- 正本と生成物の整合 ---------------------------------------------------
  run "lint_spec" python tools/lint_spec.py \
    --design "$DESIGN" --registries design-source/registries \
    --snapshot registry-snapshot.json --spec-dir spec
  run "generation_contract" python tools/check_design_generation_contract.py \
    --design "$DESIGN" --snapshot registry-snapshot.json \
    --spec-manifest spec/spec-manifest.json
  # 公開版: 合成Gitで検証器の受理・拒否契約を測る。私的な過去Reportの再現ではない。
  # 元Repositoryの実履歴Gateは非公開側に保持。docs/PUBLIC-VERIFICATION.md を参照。
  run "synthetic_consumer_contract" python tools/verify_public_history_contract.py consumer
  run "synthetic_provider_contract" python tools/verify_public_history_contract.py provider
  run "design_mirror" python tools/check_design_mirror.py
  run "design_reference_currency" python tools/check_design_reference_currency.py
  run "registry_codegen--check" python tools/generate_domain_registry_code.py --check
  run "chat_context_policy--check" python tools/generate_chat_context_policy_code.py --check
  run "core_schemas--check" python tools/build_core_schemas.py --check
  run "expectation_hashes--check" python tools/build_expectation_hashes.py --check
  run "owner_values--check" python tools/record_chat_provider_values.py --check
  run "test_manifest" python tools/validate_test_manifest.py --registries design-source/registries
  run "schema_contract" python tools/check_schema_contract.py
  run "blocked_records" python tools/check_blocked_records.py \
    --design "$DESIGN" --registry registry-snapshot.json
  run "case_coverage" python tools/check_case_coverage.py
fi

# --- Code 品質 --------------------------------------------------------------
run "forbidden_patterns" python tools/check_forbidden_patterns.py src
run "test_markers" python tools/check_test_markers.py tests
run "ruff_format--check" ruff format --check .
run "ruff_check" ruff check .

targets=()
for p in src/harness/domain src/harness/ports src/harness/infrastructure \
         src/harness/application src/harness/presentation; do
  [ -d "$p" ] && targets+=("$p")
done
run "mypy--strict" mypy --strict "${targets[@]}"
run "pytest" python -m pytest tests/ -q

echo
echo "PASS ${passed} / FAIL ${failed}"
if [ "$failed" -ne 0 ]; then
  echo "RESULT: FAILED — 手元で落ちている。Push する前に直す"
  exit 1
fi
echo "RESULT: LOCAL_OK — **GitHub CI の PASS でも Release Evidence でもない**"
exit 0
