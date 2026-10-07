# New-word

日本語IMEで使う名詞・固有名詞をカテゴリ別に収集し、converterの全辞書パックに表記が存在しない語だけを1日1件のIssueに掲載します。新語と以前から使われている未登録語を対象にし、有料AI APIは使用しません。

## 掲載条件

- 表記をNFKC・空白・英字大小で正規化し、別の読みで存在する表記も除外します。
- 独立した日本語使用例を2件以上、記事本文で確認します。通信社や同一記事の転載は1件として扱います。
- かな表記、ルビ、明示的な読み、または根拠付きの確認済み資料から完全な読みを確認します。推定読みや読みの競合は掲載しません。
- 製品・サービス・作品・人名・組織・地名などの名称は公式根拠も確認します。
- 文章、宣伝文句、不完全な名称、数字だけの文字列、入力と同じひらがな表記は除外します。動詞・形容詞の活用展開は対象外です。

一般名詞と固有名詞は別のMozc品詞に割り当てます。品詞IDは索引と同じビルドのPOS定義を使用します。最新Mozcの別途取得やGitHubコード検索は行いません。

## カテゴリと毎日の動作

`data/categories.json` に各分類の検索語と品詞種別を設定しています。

| 分類 | 対象 |
|---|---|
| 生活・食 | 日常語、料理、食品 |
| IT・AI | ソフトウェア、IT、AI |
| 科学・医療 | 科学、研究、医療 |
| 社会・ビジネス | 経済、制度、ビジネス |
| ゲーム・アニメ・音楽 | 作品、ゲーム、音楽 |
| 製品・サービス・ブランド | 製品、サービス、ブランド |
| 人名・組織 | 人物、企業、団体 |
| 地名・施設 | 地名、駅、施設 |
| ネット語・俗語 | ネット語、若者言葉、俗語 |

Google News RSSを分類ごとに検索し、24時間、30日、365日、期間制限なしの順に探索を広げます。全分類タグを保持し、設定順の最初の分類に一度だけ表示します。分類ごとに順番に選び、合計最大10語を掲載します。候補が0件ならIssueを作らず正常終了します。

Issue名は `新語候補 YYYY-MM-DD`。Toronto時間の19時に実行予定です。日付と夏時間の扱いは従来通りです。Actionsの混雑により開始が遅れる場合があります。

## 辞書の検証と初回有効化

先にconverter側の索引生成変更を反映し、次の4ファイルを同じversionリリースに公開してください。

- `japanese_keyboard_dictionary_assets.zip`
- `dictionary-index.tsv.gz`
- `dictionary-index-manifest.json`
- `dictionary-index-NOTICES.md`

最新の公開済み `v*` リリースを一度だけ選び、全13パックの収録、索引の件数とSHA-256、辞書ZIPのSHA-256、同梱POS定義との一致を検証してSQLiteに読み込みます。metadata専用リリースは選びません。新しいversionリリースに索引がない場合、古い辞書へ戻って未登録判定することはありません。

1. converterで索引付きversionリリースを公開する。
2. Actions → Daily new words → Run workflowで `check_dictionary_only` を有効にして検証する。Issueや履歴は変更しません。必要なら `dictionary_release` にタグを指定します。
3. 成功を確認し、このリポジトリのActions variable `IME_COLLECTION_ENABLED` を `true` に設定する。
4. 通常の手動収集と、次回の定時実行を確認する。

このvariableを設定するまでは定時収集は有効になりません。標準 `GITHUB_TOKEN` だけで公開辞書の読み取りとIssue・履歴の保存を行います。`CODE_SEARCH_TOKEN` は不要です。

ローカルでは `GH_TOKEN` を環境変数に設定して実行します。

```sh
python3 -m pip install -r requirements.txt
python3 scripts/collect_new_words.py --check-dictionary
python3 scripts/collect_new_words.py
```

検証用forkだけを試す場合は `DICTIONARY_REPOSITORY=owner/repository` と `DICTIONARY_RELEASE=tag` を指定できます。既定は `KazumaProject/kotlin-kana-kanji-converter` です。

## 保留と再実行

未確定語は `data/pending.json` に根拠・分類・保留理由を保存し、掲載済み語の `data/seen.tsv` と分けます。既存保留語は日ごとに巡回して最大10語再確認します。取得上限20ページの半分を保留再確認のために先に確保し、新規候補は残りの枠と記事キャッシュを共有します。記事取得は1リクエスト10秒、最大2MBです。根拠の取得に失敗しても元の出典と手動メモを保留に残します。

候補はIssueへの掲載成功後だけ履歴に記録します。応答が失われた場合も公開済みIssueから履歴を回復し、同日の合計10語を守ります。未掲載候補は保留に残し、収集失敗時も保存済みの保留と公開履歴はActionsがコミットします。

新しいメタデータはversion 2。従来のversion 1と5列TSVも読みます。既存本文・手書きメモを保持し、追加候補を分類付きで追記します。TSVには分類、確認辞書のリリース・リポジトリ・manifestのSHA-256を追加します。このTSVはレビュー候補で、辞書への自動登録や変換コストの調整は行いません。

既存Issueの読みだけを更新する場合は手動workflowの `refresh_readings`、または `python3 scripts/collect_new_words.py --refresh-readings` を使用します。

`data/readings.json` に根拠付きの確認済み読みを登録できます。同名の別作品・製品に誤適用しないよう `context` を指定してください。公式であることを確認したURLを `official_name_sources` に `sources` と同じ形式で追加できます。これも本文の名称確認と取得上限の対象です。

## テスト

```sh
python3 -B -m unittest discover -s tests -v
```

Windowsの場合:

```sh
python -m pip install tzdata
python -X utf8 -B -m unittest discover -s tests -v
```

テストは外部通信や実際のIssue作成を行わず、全パック照合、別読み・正規化、破損索引、品詞ID不一致、分類重複、転載、保留再確認、掲載失敗と旧形式互換性を検証します。
