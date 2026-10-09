# New-word

日本語IME向けに、既存辞書に未収録の**正しい名詞・名称**を集めます。以前から使われている語も対象で、有料AI APIは使用しません。

## 実行スケジュール

| Actions | 頻度 | 内容 |
|---|---|---|
| Monthly dictionary gaps | 毎月1日19:00 America/Toronto | 全ソースを探索し、未収録語を累積ZIPへ追加 |
| Weekly new words | 毎週月曜19:00 America/Toronto | 直近7日を優先して新語候補をIssueへ掲載 |

日次スケジュールはありません。両方とも手動実行できます。週次Issueは月曜の日付を使う `週間新語候補 YYYY-MM-DD` で、同じ週の再実行は同じIssueに追記し、合計10語までとします。従来のIssueと読み更新機能も保持します。週次Issueでは従来のコード検索に `CODE_SEARCH_TOKEN` が必要です。これはバイナリ辞書との収録照合ではなく、月次ZIPの照合とは独立しています。

## 対象と収集元

分類は [data/categories.json](data/categories.json) に定義します。既存の分類IDを維持し、重複する分類をすべて保存します。

生活・食 / IT / 科学・医療 / 社会・ビジネス / ゲーム・アニメ・音楽 / 製品・サービス・ブランド / 人名・組織 / 地名・施設 / ネット語・俗語 / 漢方薬 / Medicine / Software Engineering

ソースと根拠の種類は [data/sources.json](data/sources.json) で管理します。

- JMdict: 全スナップショットをストリーミング処理し、名詞の表記と読みの対応を確認します。語義ごとの表記・読み制限、品詞の継承も考慮します。
- JMnedict: 人名、地名・駅名、組織、製品、作品などの名称を同様に処理します。
- ツムラ: 公開JSON製品一覧の名称と完全なカナ読みを利用します。明記されたメーカー名の接頭辞を双方から除いた漢方処方名も照合します。
- IPA・日本語MDN: 公開用語集を辿り、見出しの語と明示的な読みを確認します。
- PMDA: PMDAドメインに限定した発見検索から公開医薬品情報を確認します。PMDA全製品の一括データを取得したという意味ではありません。
- 分類別の補助検索: 新語・新製品・発表日の制限を設けず、以前からの名称・専門用語も探します。

信頼済みの辞書レコード、または公式用語資料が表記・完全な読み・語の種類を示していれば、1ソースで採用できます。フラグだけでは採用せず、ソースID・レコードID・URL・チェックサム・読みの対応を検証します。一般の発見検索は、本文で確認した独立した日本語使用例が2件必要で、固有名詞には公式名称の根拠も必要です。記事の転載は独立した根拠として数えません。

誤表記・誤読と明記された形式は除外し、推定読み、明記のない読み、読みの競合は保留します。文章や不完全な名称も除外します。対象は名詞と名称で、動詞・形容詞の活用辞書を生成するものではありません。

## 辞書照合と配布形式

比較対象は [converter v1.7.256](https://github.com/KazumaProject/kotlin-kana-kanji-converter/releases/tag/v1.7.256) の全13パックです。固定したZIPのサイズ・SHA-256・全件読み出し結果・各パック件数を検証してからSQLite索引を使います。辞書が取得できない、壊れている、検証できない場合は処理を停止します。

**別の読みであっても同じ表記がいずれかのパックにあれば除外します。** NFKC・空白・英字大小を共通ルールで正規化します。カタカナ化や複数語を組み合わせたIME変換結果・順位は判定しません。converterそのものは変更しません。[照合方法](docs/dictionary-check.md)

同じ [累積Release](https://github.com/KazumaProject/New-word/releases/tag/new-words) の `new-words.zip` を更新します。月ごとのReleaseは作りません。

| ZIP内のファイル | 内容 |
|---|---|
| dictionary-0001.tsv 以降 | 読み・表記・Mozc品詞ラベルの3列。UTF-8、BOM・ヘッダーなし、タブ区切り、LF |
| metadata-0001.jsonl 以降 | 対応する語の分類、根拠、ソースレコード、読み確認、辞書照合、手動メモ |
| manifest.json | 形式バージョン2、Gitコミット、件数、各ファイルのサイズ・SHA-256、収集の完了状況 |
| SOURCES.txt | ソースの出典・変更内容・ライセンスの案内 |

TSVの列は変更しません。旧形式のメタデータとアーカイブも読み取れます。新メタデータは `metadata_version: 2` と `evidence_type` を持ち、辞書・公式資料には `source_records` が付きます。旧データの根拠・日時・手動メモは移行時に保持します。

JMdict/JMnedict由来のデータには [EDRDGのCC BY-SA 4.0の条件](https://www.edrdg.org/edrdg/licence.html) が適用されます。MDN由来のデータの条件もSOURCES.txtに記載し、各語の出典を保持します。ソフトウェアコードと収集データのライセンスを混同しないでください。

## 上限・再開・確認状況

月次の採用語数・保留再確認に10語や300語などの上限はありません。ソース間を順番に巡回し、同じページの確認を共有します。ホストごとのリクエスト間隔を設け、ページキャッシュはメモリ内で制限します。

1回の収集時間は4時間、Actionsジョブ全体は5時間です。途中のデータとソース進捗を定期保存します。時間に達した場合は未完了と明示して終了し、次回月次または手動 `collect` で再開します。スナップショットのチェックサム、アダプター、ソース設定が変わった場合は先頭から再照合し、重複は統合します。用語集と検索は処理済みなら翌月に更新します。

- `data/release/entries-*.jsonl`: 確定語と収録確認、`manual_notes`
- `data/release/pending.json`: 保留語、根拠、旧再確認履歴（形式2へ移行）
- `data/release/collection-state.json`: ソースのチェックサム、処理位置・URL、失敗理由、ソース別・分類別件数
- `data/readings.json`: 根拠付きで確認済みの読み

件数は処理レコード・再確認の累計で、`unfinished` は未完了ソース数です。実際の重複除去済み配布語数はmanifestの `word_count` で確認できます。読みの競合を `data/readings.json` の根拠付き確認で解消した場合は、同じ月でも手動 `collect` で再確認できます。

語のファイルは非圧縮32MiBまでで行単位に分割します。ZIPは2GiB未満を検証します。収録済みの保存語は履歴として残し、配布から除外します。読みが競合した語はメモと根拠を保留キューへ移します。

「完了」は設定したソース・スナップショットの処理完了です。保留や取得失敗は別途報告します。インターネット上のすべての語を取得済みとは表示しません。

## 初回公開と失敗からの復旧

元の失敗は、最初のRelease作成前に管理者権限の必要な設定確認APIへアクセスしたためです。設定確認は収集前には行いません。収集結果をGitへcommit・pushし、ZIPを検証してworkflow artifactへ保存した後にRelease更新を行います。

初回だけ、リポジトリ管理者が次の準備をしてください。

1. immutable releasesが無効であることを確認します。
2. タグ `new-words` の更新可能なReleaseを、添付ファイルなしで公開します。下書きのままにしないでください。
3. Actions → **Monthly dictionary gaps** → Run workflow → `mode: rebuild` を実行します。

以降は標準の `GITHUB_TOKEN` の `contents: write` で更新できます。管理者トークンをActionsへ追加する必要はありません。リポジトリ設定を自動で変更しません。

初回設定、immutable Release、アップロード失敗で公開できなくても、保存済みのGitデータと検証済みZIPは残ります。Actionsの `dictionary-gaps-<run_id>` artifactは30日保持します。原因を解消したら `rebuild` で再配布できます。`rebuild` はニュース・辞書ソースを収集せず、保存語を照合して同じZIPを再アップロードします。

通常は内容が変わらなければZIPの置換を省略します。置換は旧Assetを削除してからアップロードするため、アップロード失敗時は一時的にZIPが取得できなくなる可能性があります。Gitから再構築できます。fork・PRの検証ではIssue・Releaseを公開しません。

## ローカル検証

Python 3.12、Java 17、GitHub CLIを使用します。Windowsでは `tzdata` が必要な場合があります。

```sh
python -m pip install -r requirements.txt
python -B -m unittest discover -s tests -v
export DICTIONARY_INDEX_PATH=.cache/dictionary.sqlite
python scripts/dictionary_assets.py --output "$DICTIONARY_INDEX_PATH"
python scripts/collect_release_words.py --mode rebuild
python scripts/release_archive.py
```

ローカルで月次収集を開始するには `--mode collect` を使います。短い再開検証には `--budget-seconds 60` などを指定できます（最大14400秒）。辞書索引・ソースのダウンロード・生成ZIPはGit管理外です。週次Issueの読みだけの更新は従来の `--refresh-readings` を使用できます。
