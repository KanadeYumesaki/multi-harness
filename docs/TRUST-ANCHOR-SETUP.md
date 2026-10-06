# Trust Anchor 確立手順書

作成日: 2026-08-15
対象: MVP0-A 以降の Release GO
関連: `docs/MVP0-A_Runtime_GO再開Runbookと未実装優先順位.md` §3.4、`tools/verify_trust_anchor.py`
補足: `docs/PUBLIC-RELEASE-OPERATION.md`（公開用の現行運用規則。旧個別運用記録は非公開で保持）

> **配布時の初期状態:** `ci/allowed-signers` に信頼する鍵は1件もありません。
> 以前の所有者の鍵を既定で信頼しないためです。この状態では
> `tools/verify_trust_anchor.py` は「信頼する鍵が無い」理由でどのTagも拒否し、
> 判定は `UNTRUSTED_REVIEW_ONLY` のままです。以下は利用者が自分の鍵で
> 信頼根を作るときの手順です。例示の値は自分の値へ置き換えてください。

---

## 0. なぜ必要か（先に読む）

`release-binding.json` の `commit_ancestry` は、ZIP生成時に
`git merge-base --is-ancestor` で確かめた結果を**記録**しています。
しかしZIPはGit object databaseを含まないため、受領者はその記録を
再証明できません。**記録を書いたのが誰かも分かりません。**

```json
"commit_ancestry": {
  "verified_ancestors": ["3de24e8e..."],
  "verification_method": "git merge-base --is-ancestor",
  "external_trust_anchor_required": true,
  "note": "ZIP omits Git objects; archive-only verification checks this record but cannot prove ancestry."
}
```

`external_trust_anchor_required: true` は「これだけでは足りない」という
正直な自己申告です。レビューが Release GO を NO-GO とした理由もここにあります。

**署名付きTagは、この自己申告に「誰が」を付けます。** Tagの署名が検証できれば、
そのCommitをReleaseとして指したのが鍵の持ち主だと確かめられます。

### 署名は、検証されて初めて意味を持つ

署名を作るだけなら誰でもできます。攻撃者は自分の鍵で署名したTagを付けられます。
**どの鍵を信じるかを先に決めて、そこから外れた署名を落とす**仕組みが無ければ、
署名は飾りです。本手順書はその両方を揃えます。

---

## 1. 方式の選択

必要な道具は次のとおりです。版は各自の環境で確かめてください。

```text
git       2.34 以降  （SSH署名に対応）
OpenSSH   ssh-keygen -Y sign / -Y verify が使えること
GnuPG     GPG署名は検証器が受け付けない（§1）
```

| | SSH署名（推奨） | GPG署名 |
|---|---|---|
| 鍵の作成 | `ssh-keygen` 1コマンド | 鍵束・有効期限・失効証明書の管理が要る |
| GitHubのVerifiedバッジ | 対応（Signing key として別途登録） | 対応 |
| オフライン検証 | `allowed-signers` ファイルだけで完結 | 公開鍵の配布経路が別途要る |
| 失効 | ファイルから削除（履歴に残る） | 失効証明書の配布が要る |
| 単独開発での運用負荷 | 低い | 高い |

**SSH署名を推奨します。** 単独開発で、検証がGit以外に依存せず、
`allowed-signers` の変更がCommitとして残るためです。

> GPG署名は `tools/verify_trust_anchor.py` が受け付けません。
> `gpg.ssh.allowedSignersFile` はGPG鍵を制約せず、ローカルkeyringの任意の鍵で
> 検証が通りうるためです（検証器のDocstring参照）。

---

## 2. 署名鍵の作成（あなたの作業）

**鍵は秘密です。私は作りません。** 以下はあなたが実行します。

### 2.1 認証用とは別の鍵を作る

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
ssh-keygen -t ed25519 -C "release-signing (fde-harness)" -f ~/.ssh/fde_release_signing
chmod 600 ~/.ssh/fde_release_signing
```

パスフレーズは**設定してください**。Release Tag は年に数回しか作らないので、
毎回入力する負担より、鍵ファイルが流出したときの被害の方が大きくなります。

> **なぜ認証鍵を流用しないのか**
> 認証鍵は接続のたびに使われ、agent forwarding で他所へ渡ることもあります。
> 署名鍵は「このCommitをReleaseとして承認した」という意思表示に使います。
> 用途が違う鍵を分けておくと、片方を失効させても、もう片方の意味が
> 変わりません。

### 2.2 Git に署名鍵を教える

```bash
cd ~/fde-harness
git config --local gpg.format ssh
git config --local user.signingkey ~/.ssh/fde_release_signing.pub
git config --local tag.gpgSign true          # tag -a で作っても署名される
git config --local gpg.ssh.allowedSignersFile "$PWD/ci/allowed-signers"
```

`--local` にしています。他のRepositoryへ影響させないためです。

### 2.3 信頼する鍵を Repository へ登録する

```bash
cd ~/fde-harness
printf '%s %s\n' "<署名者のPrincipal（例: user.email）>" "$(cat ~/.ssh/fde_release_signing.pub)" \
  > ci/allowed-signers
cat ci/allowed-signers
```

**作ったらCommitします。** 追跡されていないと `git tag -v` が
`gpg.ssh.allowedSignersFile needs to be configured and exist` で落ちます。
受領者がbundleから検証する経路でもこのFileを使うため、配布物へ入る
必要があります。

```bash
git add ci/allowed-signers
git commit -m "chore(release): Release署名の信頼鍵を登録"
```

> **Repositoryを改ざんできる者はここも書き換えられるのでは？**
> そのとおりです。ここで防いでいるのは改ざんではなく、**鍵の追加が
> 見えなくなること**です。信頼する鍵が1行増えれば差分に出ます。
> Branch保護とReviewの対象になります。

### 2.3b ssh-agent へ鍵を預ける（任意・推奨）

パスフレーズ付きの鍵は、署名のたびに入力を求められます。Release作業は
Tag作成・ZIP生成・検証と数回署名するので、Session中は預けておくと楽です。

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/fde_release_signing     # ここで1回だけパスフレーズを入力
ssh-add -l                             # 預かっていることを確認
```

Terminalを閉じると消えます。毎回打つのが煩わしければ `~/.bashrc` へ
agentの起動だけ書き、`ssh-add` は作業時に手で実行してください。
**`~/.bashrc` へパスフレーズを書かないこと。** それをするなら
最初からパスフレーズを付けない方がまだ被害が読めます。

### 2.3c 外部固定Hashを Protected Variable へ登録する（**必須**）

**ここまでの設定だけでは Trust Anchor は成立しません。**

Tagが指すCommitの中に、検証器（`tools/verify_trust_anchor.py`）も
許可鍵（`ci/allowed-signers`）も入っています。悪意あるCommitは
自分の鍵を許可鍵へ足し、検証器を「常に成功」へ書き換え、そのCommitへ
署名したTagを打てます。Repository内の整合性は保たれているので、
内部の検査では気付けません。

**署名は「誰が」を示しますが、その誰かを誰が決めるのかは署名の外にあります。**

そこで、両Fileの期待HashをRepositoryの外側（GitHub の Variables）へ置きます。

```bash
cd ~/fde-harness
python - <<'PY'
import hashlib, pathlib
for path in ("tools/verify_trust_anchor.py", "ci/allowed-signers"):
    digest = hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
    print(f"{path}\n  sha256:{digest}\n")
PY
```

出た値を `https://github.com/<OWNER>/<REPO>/settings/variables/actions`
へ登録します。**Secrets ではなく Variables** です。値は公開Hashであり、
CIのログにも出ます。画面上は Secrets ページ内の**タブ**なので、
サイドバーを探しても見つかりません。

| Variable名 | 値 |
|---|---|
| `TRUST_ANCHOR_VERIFIER_SHA256` | `tools/verify_trust_anchor.py` の `sha256:...` |
| `TRUST_ANCHOR_SIGNERS_SHA256` | `ci/allowed-signers` の `sha256:...` |

コマンドでも登録できます。**値は貼らずに実測させます。** 手で貼ると、
貼り間違いに気付けるのがCIの失敗時になります。

```bash
cd ~/fde-harness
test -z "$(git status --porcelain)"   # 汚れているとHEADと値がずれる
gh variable set TRUST_ANCHOR_VERIFIER_SHA256 --body \
  "sha256:$(sha256sum tools/verify_trust_anchor.py | cut -d' ' -f1)"
gh variable set TRUST_ANCHOR_SIGNERS_SHA256 --body \
  "sha256:$(sha256sum ci/allowed-signers | cut -d' ' -f1)"
gh variable list
```

> **この2つが未設定だと、CIの `release-tag-signature` は失敗します。**
> `--require-external-anchor` を付けているためです。未設定のまま通ると、
> 外部固定が無いことに気付かないまま「CIが緑」になります。
>
> 検証器や許可鍵を**意図して**変更したときは、Variableも更新してください。
> 更新を忘れれば失敗します。それが正しい挙動です。改変とは区別できません。

### 2.3d Protected Tag と Branch保護 — **現Planでは使えない**

GitHubのPlanやRepositoryの公開設定によっては、次のように保護設定のAPIが使えません。

```text
GET repos/<OWNER>/<REPO>/branches/main/protection  -> 403
GET repos/<OWNER>/<REPO>/rulesets                  -> 403
  "Upgrade to GitHub Pro or make this repository public to enable this feature."
```

**利用できないPlanでは、Branch保護もTag保護も設定できません。** 利用前に現在の提供条件を確認してください。
手順として書いても実行できないので、選択肢を先に示します。

| 選択肢 | 得られるもの | 代償 |
|---|---|---|
| A. GitHub Pro へ更新 | Branch保護・Ruleset・Protected Tag | 月額 |
| B. Repositoryを公開 | 同上（Publicは無料で使える） | 設計書・実装が公開される |
| C. 現状維持 | CI検査と署名検証は動く | `UNTRUSTED_REVIEW_ONLY` のまま |

### CI検証の限界（Cを選ぶ場合に理解しておくこと）

Variables はBranch保護が使えないPlanでも設定できます。しかし
**GitHub Actions は「Tagが指すCommitに入っているWorkflow定義」を実行します。**

つまり悪意あるCommitは `.github/workflows/ci.yml` から
`--require-external-anchor` を消すこともできます。Variableを守っても、
Variableを読む側のコードが書き換えられれば意味がありません。

Branch保護とRulesetが無い環境では、**CIによるTrust Anchorは
原理的に自己参照から抜け出せません。** これは実装の不足ではなく、
Planの制約です。

### だから受領側検証が本体である

Cを選ぶ場合、Trust Anchor の実体はCIではなく**受領者がオフラインで行う
検証**になります。

```bash
cd <配布物を置いたDirectory>
sha256sum -c SHA256SUMS
git clone trust-anchor.bundle verify-repo && cd verify-repo
git -c gpg.ssh.allowedSignersFile=ci/allowed-signers tag -v <RELEASE_TAG>
```

この経路はGitHubを一切信頼しません。必要なのは次の1点だけです。

> **署名鍵のFingerprintを、Repositoryの外で受領者へ伝えてあること。**

```text
<署名鍵のコメント>
ED25519  SHA256:<署名鍵のFingerprint>
```

これをRepositoryの外（名刺、署名付きメール、個人サイト、契約書の附属書など）
で伝えます。受領者が `ci/allowed-signers` に載っている鍵とこのFingerprintを
突き合わせられれば、そこが Trust Anchor になります。

**Repository内のどのFileも、この役割を果たせません。** Repositoryごと
書き換えられる相手に対して、Repository内の記述は根拠になりません。

> **A / B を選ぶまでは、Release GO を宣言しないでください。**
> 検証結果は `UNTRUSTED_REVIEW_ONLY` です。開発中の確認としては有効ですが、
> 独立したTrust Anchorではありません。

### 2.4 GitHub へ「署名鍵として」登録する

認証鍵として登録済みでも、**署名鍵としては別に登録が要ります**。
登録しないとGitHub上で "Unverified" と表示されます（検証自体はローカルで通ります）。

Web UI:

1. <https://github.com/settings/keys> を開く
2. **New SSH key**
3. Key type で **Signing Key** を選ぶ（既定は Authentication Key）
4. `~/.ssh/fde_release_signing.pub` の内容を貼る

> gh CLI でも登録できます。Tokenに `admin:ssh_signing_key`
> scope が無い場合は、
> `gh auth refresh -h github.com -s admin:ssh_signing_key` で追加してから
> `gh ssh-key add ~/.ssh/fde_release_signing.pub --type signing` が使えます。
> Web UI の方が手数が少ないので、そちらを勧めます。

### 2.5 動作確認

```bash
cd ~/fde-harness
git tag -s trust-anchor-smoke -m "signature smoke test"
python tools/verify_trust_anchor.py --tag trust-anchor-smoke
git tag -d trust-anchor-smoke
```

`trust anchor ok: tag=... signer=<署名者のPrincipal>` と出れば成立です。
出なければ §5 を見てください。

> この確認は**対話端末で実行してください**。パスフレーズの入力が要ります。
> Script やCIから流すと入力待ちで止まります（`timeout` 無しだと固まります）。

---

## 3. Release Tag を打つ（Releaseのたび）

### 3.1 前提

* 作業ツリーがクリーン（`git status --short` が空）
* CI が緑
* Release対象のCommitが `origin/main` にある

### 3.2 手順

```bash
cd ~/fde-harness
RELEASE_TAG="mvp0a-$(date -u +%Y%m%d)-$(git rev-parse --short HEAD)"
RELEASE_COMMIT="$(git rev-parse HEAD)"

test -z "$(git status --porcelain --untracked-files=all)"

git tag -s "$RELEASE_TAG" -m "MVP0-A release ${RELEASE_COMMIT}"
python tools/verify_trust_anchor.py \
  --tag "$RELEASE_TAG" \
  --expect-commit "$RELEASE_COMMIT" \
  --emit-evidence "evidence/trust-anchor/trust-anchor.json"

git push origin "$RELEASE_TAG"
```

`verify_trust_anchor.py` の終了Codeは次のとおりです。

| Code | 意味 | 扱い |
|---:|---|---|
| 0 | 署名を検証できた | Trust Anchor 成立 |
| 2 | 署名が無い | **未確立**。BLOCKED に相当する正常な未達 |
| 3 | 署名はあるが信頼できない／別Commitを指す | **異常**。原因を特定するまで進まない |
| 4 | 入力不正 | 引数を直す |

2 と 3 を分けているのは、「まだ張っていない」と「壊れている」を
同じ見た目にしないためです。

### 3.3 Release ZIP へ結び付ける

`--required-ancestor` には**HEADではなく、そのReleaseに必ず入っていて
ほしい修正のCommit**を渡します。HEAD自身を渡すと「自分は自分の祖先である」
という自明な条件になり、何も確かめたことになりません。

```bash
# 例: 必ず含めたい修正のCommitを条件にする
REQUIRED_ANCESTOR="$(git rev-parse <必須の修正Commit>)"

python tools/package_release_zip.py \
  --source-root . \
  --out "$HOME/dist/${RELEASE_TAG}.zip" \
  --required-ancestor "$REQUIRED_ANCESTOR" \
  --trust-anchor-tag "$RELEASE_TAG" \
  --verify
```

`--trust-anchor-tag` を渡すと、Tagの署名を検証したうえで
`release-binding.json` へ記録します（`binding_version: "1.3"`）。

```json
"trust_anchor": {
  "tag": "mvp0a-...",
  "signer": "<署名者のPrincipal>",
  "verification_method": "git tag -v",
  "allowed_signers_path": "ci/allowed-signers"
}
```

> **`external_trust_anchor_required` は true のままです。**
> 署名があっても、ZIPはGit objectを含まないので archive-only では
> 祖先を証明できません。変わるのは「証明できる」ことではなく、
> **どこを見れば証明できるか**が分かることです。ここを false へ倒すと、
> archive-only 検証のPASSを「祖先まで確かめた」と読み違える余地ができます。
>
> Tagを渡さない場合、`trust_anchor` は `null` になります。**欄ごと
> 省きません。** 省くと受領者は「Anchorが無い」のか「古い生成器で欄が
> 無い」のかを区別できません。

複数指定できます。「この修正が抜けたReleaseを作らない」という条件を、
Releaseごとに人の記憶ではなくコマンドへ書き出す欄です。

> **落とし穴: 鍵と配布物をRepositoryの中へ置かない**
> `package_release_zip.py` は clean な Git 木を要求します。Repository内に
> 鍵・ZIP・bundle・展開先を作ると、その時点で untracked が増えて
> **ZIP生成が落ちます**。手順書を通しで実行したとき実際に踏みました。
>
> * 鍵     → `~/.ssh/`
> * 配布物 → `~/dist/<RELEASE_TAG>/`
> * Evidence → `evidence/`（`.gitignore` 済み。ここだけは中でよい）

### 3.4 配布物の SHA256SUMS（相対Pathで作る）

```bash
DIST="$HOME/dist/${RELEASE_TAG}"
mkdir -p "$DIST"
cp "/tmp/${RELEASE_TAG}.zip" "$DIST/"
git bundle create "$DIST/trust-anchor.bundle" --all
git bundle verify "$DIST/trust-anchor.bundle"

cd "$DIST"
sha256sum "${RELEASE_TAG}.zip" trust-anchor.bundle > SHA256SUMS
cat SHA256SUMS
```

受領側:

```bash
cd <配布物を置いたDirectory>
sha256sum -c SHA256SUMS
git clone trust-anchor.bundle verify-repo
cd verify-repo
git tag -v <RELEASE_TAG>          # ci/allowed-signers はbundle内にある
```

---

## 4. 不変保管（Immutable Evidence Store）

署名は「誰が承認したか」を示しますが、「あとから差し替えられていないか」は
別の話です。鍵を持つ本人が古いTagを消して打ち直せます。

不変保管は、**書いた本人でも消せない場所**へ記録を置くことで、そこを塞ぎます。
運用規模に応じて選んでください。

### 選択肢A: GitHub Releases（すぐ始められる）

Private Repository でも使えます。GitHub 側に保管され、資産の削除は
監査ログに残ります。

```bash
gh release create "$RELEASE_TAG" \
  "$DIST/${RELEASE_TAG}.zip" \
  "$DIST/trust-anchor.bundle" \
  "$DIST/SHA256SUMS" \
  --title "$RELEASE_TAG" \
  --notes "MVP0-A release. commit=${RELEASE_COMMIT}"
```

> **Artifact Attestation について（未確認）**
> GitHub Actions の `actions/attest-build-provenance` を使うと
> 「どのWorkflow実行がこのZIPを作ったか」がGitHub側に残ります。ただし
> **Private Repository で利用できるかはPlanに依存**し、この環境では
> 確認していません。導入前に
> <https://docs.github.com/actions/security-guides/using-artifact-attestations>
> で現在の提供条件を確認してください。使えるなら、Workflowへ次を足します。
>
> ```yaml
> permissions:
>   id-token: write
>   attestations: write
>   contents: read
> steps:
>   - uses: actions/attest-build-provenance@v1
>     with:
>       subject-path: 'dist/*.zip'
> ```

> **限界**: GitHub組織の管理者権限があれば消せます。単独開発では
> あなた自身が管理者なので、「本人でも消せない」までは到達しません。
> 選択肢B・Cはそこを埋めます。

### 選択肢B: Object Storage の Object Lock（WORM）

外部監査や顧客提出が要る段階になったら移行します。

```bash
# 例: S3 互換ストレージ。Bucket作成時に Object Lock を有効化しておく
aws s3api put-object \
  --bucket fde-harness-evidence \
  --key "releases/${RELEASE_TAG}/${RELEASE_TAG}.zip" \
  --body "$DIST/${RELEASE_TAG}.zip" \
  --object-lock-mode COMPLIANCE \
  --object-lock-retain-until-date "$(date -u -d '+7 years' +%Y-%m-%dT%H:%M:%SZ)"
```

`COMPLIANCE` モードは**root権限でも保持期間中は削除できません**。
ここまで来ると「本人でも消せない」が成立します。

### 選択肢C: 公開Timestamp（無料・補助的）

保管ではなく「その時刻に存在したこと」だけを外部に固定します。

```bash
# RFC 3161 Timestamp Authority へ SHA-256 を提出する例
openssl ts -query -data "$DIST/SHA256SUMS" -sha256 -cert -out request.tsq
curl -s -H "Content-Type: application/timestamp-query" \
  --data-binary @request.tsq https://freetsa.org/tsr > response.tsr
openssl ts -reply -in response.tsr -text | head -20
```

`response.tsr` を配布物へ同梱します。あとから内容を変えても、
Timestampと合わなくなります。

### 現段階の推奨

**選択肢A + C** から始めてください。追加費用も外部契約も要りません。
外部監査が要求された時点で B へ移ります。

### 4.x Release完了条件（**CI Artifactは長期保管ではない**）

CIの `release-tag-signature` は成果物を Artifact として残しますが、
これは GitHub Actions の保持期限（90日）で消えます。**保管ではありません。**

Releaseを「完了」と呼ぶには、次の5点が不変保管先へ入っていることを
条件とします。どれか1つでも欠けている間は未完了です。

| # | 成果物 | 出どころ |
|---:|---|---|
| 1 | Release ZIP | `package_release_zip.py` |
| 2 | Git bundle | `git bundle create` |
| 3 | `trust-anchor.json` | `verify_trust_anchor.py --emit-evidence` |
| 4 | `SHA256SUMS` | §3.4（相対Path） |
| 5 | `release-binding.json` | ZIP内。単体でも取り出して保存する |

保管先は §4 の A（GitHub Release）以上。外部監査要件があるなら B。

---

## 5. うまくいかないとき

| 症状 | 原因 | 対処 |
|---|---|---|
| `gpg.ssh.allowedSignersFile needs to be configured` | §2.2 の最終行が未設定 | `git config --local gpg.ssh.allowedSignersFile "$PWD/ci/allowed-signers"` |
| `No principal matched` | `ci/allowed-signers` に鍵が無い／メールアドレスが違う | 左端をコミット時の `user.email` と一致させる |
| GitHub上で "Unverified" | 署名鍵として未登録（§2.4） | Key type を **Signing Key** で登録し直す |
| `error: gpg failed to sign the data` | 秘密鍵のPathが違う／権限が広い | `chmod 600 ~/.ssh/fde_release_signing` |
| 終了Code 2 のまま | lightweight tag になっている | `git tag -s`（`-a` ではなく）で作り直す |

---

## 6. 配布時の組み込み状況

以前の所有者の署名鍵・Protected Variable・実測記録はこの配布物に含めません。
配布時点の状態は次のとおりです。

| # | 項目 | 状態 |
|---:|---|---|
| 1 | 署名鍵の作成 | 未実施（利用者が §2 で作る） |
| 2 | `git config`（`gpg.format=ssh` ほか） | 未実施 |
| 3 | `ci/allowed-signers` への鍵の登録 | **未登録**（注釈のみ） |
| 4 | Protected Variable 2件の登録 | 未実施 |
| 5 | CI に Tag署名の検証Job | 定義済み（外部固定が無ければ失敗する） |
| 6 | `release-binding.json` へ Trust Anchor を記録（`1.3`） | 実装済み |
| 7 | Branch保護 / Protected Tag | 未確認（Planと設定に依存） |
| 8 | 署名鍵Fingerprintの外部公表 | 未実施 |
| 9 | Evidence Area へ `trust_anchor` を追加 | **禁止**（C運用の決定） |
| 10 | 実Tagを押してのWorkflow実測 | 未実施 |

現時点の総合判定は **`UNTRUSTED_REVIEW_ONLY`** です。信頼する鍵が無い間は
検証器がどのTagも拒否します。鍵を登録しても、8 と外部固定が揃うまでは
独立したTrust Anchorではありません（§2.3c、§2.3d）。

Release運用の規則は `docs/PUBLIC-RELEASE-OPERATION.md` にあります。
本書は鍵と設定の作り方を扱います。

> **CIの成果物をRelease根拠にしないでください。** CIは検査と生成の補助です。
> 判定の根拠は、受領者が外部伝達されたFingerprintとbundleで行うオフライン
> 検証にあります。

### 1. CI

`refs/tags/*` を押したときだけ走ります。通常のpushでは走りません。
Tagがない状態で走らせても毎回同じ結果になるだけで、何も見ていない工程に
なるためです。

未署名Tag（終了Code 2）も**ここでは失敗**にします。Tagを押すのは
Releaseの意思表示であり、「まだ張っていない」で通してよい場面ではありません。

### 3. Evidence Area の追加は保留

`trust_anchor` を14番目の Evidence Area にするには
`design-source/registries/` の正本を変えることになります。そうすると

* `registry-snapshot.json` の Hash
* 設計書に書かれた領域数（13）
* spec shard と README の Hash

が連動して変わります。**これは実装ではなく設計変更**なので、独断では
入れません。レビューでの承認をいただいてから、`apply_design_v18.py`
経由で正本ごと再生成します。

`verify_trust_anchor.py --emit-evidence` は Evidence Area と同じ形式の
JSONを出しますが、**Runtime GO の Evidence ではありません。**

> `trust-anchor.json` は Release 補助記録であり、Runtime GO の
> Evidence Area を満たすものではない。

取り違えを防ぐため、出力自体へ次を書いています。

```json
"runtime_go_evidence_area": false,
"trust_level": "EXTERNALLY_ANCHORED" | "UNTRUSTED_REVIEW_ONLY"
```

`trust_level` は外部固定Hash（§2.3c）を渡したかで決まります。
同じ「PASS」でも意味が違うので、区別できる形で残します。

---

## 7. 用語

| 用語 | 意味 |
|---|---|
| Trust Anchor | 検証の起点。ここだけは他の何かで検証せず「信じる」と決める対象 |
| annotated tag | Git objectとして実体を持つTag。署名を載せられる |
| lightweight tag | Commitへの別名にすぎないTag。署名を載せられない |
| allowed-signers | OpenSSHの署名検証で「この鍵を信じる」を宣言するファイル |
| WORM | Write Once Read Many。書いたあと消せない保管方式 |
| attestation | 「誰が・どの手順で・何を作ったか」の署名付き証明 |
