<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

## 1.11 Canonical JSON／Hash規約

Plan、Approval、Policy、Receipt、Schema SetのHashは以下で固定する。

* Canonical JSON：RFC 8785 JCS準拠
* 文字コード：UTF-8
* Unicode：入力検証前にNFCへ正規化。ただしArtifact Binary Hashは原Bytes
* 数値：JCS表現。NaN、Infinity、負の0、実装依存Decimalを禁止
* 重複Object Key：Parse時に拒否
* 不正Unicode、未対Unicode Surrogate：拒否
* 改行：Text Artifactの論理正規化はLF、Content Hashは保存BytesのHash
* Hash Algorithm：SHA-256
* Domain Separation：`FDE-HARNESS/<artifact-type>/<schema-major>/`をCanonical Bytesの前へ付加
* Hash Profile Version：`hash_profile_version=1`
* Path：OS別BrokerまたはLinux Directory Handle基準で生成したWorkspace相対Canonical Path
* Secret値：対象外。Secret Reference ID、Version、Auth Route、Account Scopeだけを含める
* Timestamp：意味のあるSnapshot時刻はContentへ含める。Run発行時刻、表示時刻、Approval操作時刻はAuthority Envelope側へ分離する
* Schema Set：各Schema ID／Version／HashのCanonical配列を含める

### 1.11.1 Plan Hashの二層分離

Planの再現性とApprovalの単回Authority束縛を同一Hashへ混在させない。次の2つを正本とする。

| Hash | 目的 | 含める | 含めない |
|---|---|---|---|
| `plan_content_hash` | 意味的なPlan再現性、差分検証、Cache Key | Action Graph、Context、Input Read Evidence、Runtime／Invocation仕様、Provider／Model／Auth／Account／Entitlement／Pricing、Policy／Schema／Token Snapshot、Resource上限、Effect内容 | `run_id`、`execution_plan_id`、`plan_version`、Plan発行時刻、`expires_at`、Approval情報 |
| `execution_plan_hash` | 個別Runの実行Authority、Approval束縛、Replay防止 | `plan_content_hash`、`run_id`、`execution_plan_id`、`plan_version`、`issued_at`、`expires_at`、Planner Identity、Authority Scope、Hash Profile Version | Signature Value、表示用要約、Secret値 |

計算式：

```text
plan_content_hash =
  SHA-256(
    "FDE-HARNESS/plan-content/1/" UTF-8 bytes
    || RFC8785-JCS(PlanContentProjection)
  )

execution_plan_hash =
  SHA-256(
    "FDE-HARNESS/execution-plan-authority/1/" UTF-8 bytes
    || RFC8785-JCS({
         "plan_content_hash": plan_content_hash,
         "run_id": run_id,
         "execution_plan_id": execution_plan_id,
         "plan_version": plan_version,
         "issued_at": issued_at,
         "expires_at": expires_at,
         "planner_identity": planner_identity,
         "authority_scope": authority_scope,
         "hash_profile_version": hash_profile_version
       })
  )
```

不変条件：

* 同一ホスト・同一Workspace上で、同一Frozen PlanBuildInput、同一Policy、同一Capability／Entitlement／Pricing／Token／Schema Snapshot、同一Planner Algorithm Versionからは同一`plan_content_hash`を生成する。ホストを跨いだ一致は保証しない。
* `run_id`、`execution_plan_id`、`plan_version`、`issued_at`、`expires_at`のいずれかが異なるAuthority Envelopeは、同一Contentでも異なる`execution_plan_hash`を生成する。
* ApprovalGrantは`execution_plan_hash`へ束縛し、監査・差分表示用に`plan_content_hash`も保持する。
* Content変更は両Hashを変更する。Authorityだけの再発行は`plan_content_hash`を維持し、`execution_plan_hash`を変更する。
* `AT-PLAN-DETERMINISM-001`は、Content Hash同値とAuthority Hash差異を別Assertionとして検証する。
* Plan再生成時に同一`plan_content_hash`が得られない場合、差分を構造化して`PLAN_NONDETERMINISTIC`で停止する。

Hash Algorithm、Domain Separation、Canonicalization Version、Plan Content Projectionのいずれかを変更する場合はMajor変更とし、旧Plan／Approvalを再利用しない。

Plan Builderは次のFrozen Inputだけを受け取る純粋関数とする。

```python
@dataclass(frozen=True)
class PlanBuildInput:
    intent_hash: str
    workspace_snapshot_hash: str
    input_read_evidence_hash: str
    context_bundle_hash: str
    policy_snapshot_hash: str
    token_profile_hash: str
    schema_set_hash: str
    capability_snapshot_hash: str | None
    entitlement_snapshot_hash: str | None
    normalized_actions: tuple[NormalizedAction, ...]
```

Workspace、Clock、UUID、Policy Store、Filesystem、EnvironmentをBuild中に再読込してはならない。外部状態を1回だけ取得して`PlanBuildInput`へ凍結した後、`build(input)`を2回実行する。`plan_content_hash`またはCanonical Projectionが異なる場合は`PLAN_NONDETERMINISTIC`で停止する。設定`plan_determinism_double_build`は既定`true`であり、無効化はTest環境の明示Fixture以外で許可しない。

**同一プロセス内の二重Buildだけでは不十分である。** Pythonの`str`のHash値は`PYTHONHASHSEED`に依存し、プロセス内では固定される。したがってPlannerに`set`／`frozenset`の反復が1箇所でも混入した場合、二重Buildは常に一致し、次回起動時に`plan_content_hash`が変化する。この最も発生しやすい非決定性を二重Buildは原理的に検出できない。

そのため次の3層で強制する。

| 層 | 内容 | 検出できるもの |
|---|---|---|
| 同一プロセス二重Build | `build(input)`を2回実行しHash一致を確認 | 時刻・乱数・UUID・可変状態の混入 |
| **別プロセス三重Build** | `PYTHONHASHSEED`を変えた子プロセスで3回目をBuildし、Hash一致を確認 | Hash順序依存（`set`反復、`hash()`利用） |
| **静的検査** | Planner Packageにおける`set`／`frozenset`の反復、`dict`順序への暗黙依存、`sorted`なしの`.keys()`／`.values()`／`.items()`反復をAST検査で拒否 | 上記の混入そのもの |

Plan Build文脈では次のProcess環境を固定し、`PlanBuildInput`ではなく`RuntimeAttestation`と`plan_build_environment_hash`へ記録する。

```text
PYTHONHASHSEED  : 三重Buildで意図的に変える。通常実行では固定しない
LC_ALL          : C.UTF-8
LANG            : C.UTF-8
TZ              : UTC
PYTHONUTF8      : 1
```

Locale依存の大小比較（`str.lower()`、`locale.strcoll`、`sorted`のkeyなし比較）をPlan Content生成へ使用しない。整列はUnicode Code Point昇順を明示する。`AT-PLAN-DETERMINISM-001`は`CROSS_PROCESS`Caseで別`PYTHONHASHSEED`によるHash一致を検証する。

`spec/01-plan-content.md`または対応Registryには全Projection Fieldについて「決定的／非決定的、根拠、採否」を記録し、台帳にないFieldをHash入力へ追加しない。

### 会話SnapshotのHash束縛（v1.20）

Owner Decision `CHAT-2-A`により、会話のContext SnapshotはPlan Contentと同じ決定論規則を使う。別規則を作らない。

* SnapshotのContentへランダムID、採番ID、時刻、PID、Filesystem列挙順を含めない（不変条件#4）
* SnapshotはHashだけを含める。参照先の本文を埋め込まない
* 1回凍結した入力から2回Buildし、さらに別`PYTHONHASHSEED`の子プロセスで3回目をBuildしてHash一致を検証する（不変条件#5）
* `set`／`frozenset`を反復しない。Code Point昇順で`sorted`してから使う（不変条件#6）
* 同一履歴から同一Hashが得られる。`PYTHONHASHSEED`に依存しない
* Build手順は設計と試験で同一のものを使う。試験側へ別実装を置かない

Snapshotは次の3つへ束縛する。どれか1つでも動けばSnapshotは別物である。

| 束縛先 | 意味 |
|---|---|
| Message集合のHash | どの会話履歴から作ったか |
| 規範Schema SetのHash（`schema_catalog_hash`） | どのSchemaで解釈したか |
| 設計正本のHash（`design_sha256`） | どの契約のもとで作ったか |

**Message集合の正規化規則と順序キーは未確定である。** Messageの必須Fieldが決まって
いないため、何を正規化し何で並べるかを書けない。`docs/decision/DCR-CHAT-CONTRACT.md`の
`CC-3`／`CC-4`で決める。決まるまでSnapshotのBuild手順を実装しない。採番IDと時刻は
順序キーにできない（不変条件#4）。
