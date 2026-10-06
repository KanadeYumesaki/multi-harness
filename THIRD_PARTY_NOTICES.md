# 第三者データ・ライセンスの案内

## Unicode Character Database

`src/harness/masking/ucd/14.0.0-assigned-codepoints` は Unicode Character Database 14.0.0 の割当情報から生成したBitmapです。生成器は `tools/build_ucd_assigned_bitmap.py` です。

- 原データの案内: https://www.unicode.org/Public/14.0.0/ucd/ReadMe.txt
- 原案内のコピー: [Unicode 14.0.0 ReadMe](LICENSES/Unicode-14.0.0-ReadMe.txt)
- 公式の利用許諾: https://www.unicode.org/license.txt
- 許諾文のコピー: [Unicode License](LICENSES/Unicode-LICENSE.txt)

許諾文は2026-09-28取得時点の公式 Unicode License V3（1991–2026）です。14.0.0公開当時の許諾文の版を再現したものとは扱いません。原ReadMeの著作権表示（2021）もそのまま保持しています。配布時はこれらの表示と許諾文も同梱してください。

## Python依存と外部CLI

Python依存の台帳は `ci/license-exceptions.yaml` と `tools/check_licenses.py` とHash固定requirementsを参照してください。SBOMはCIで生成します。ProviderのCLI、OS、ブラウザーはこのソースの同梱物ではありません。それぞれの提供元の利用条件・配布条件を別途確認してください。

この案内は各依存の利用許諾全文や包括的な法的適合確認の代わりにはなりません。
