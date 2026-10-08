# New-word

日本語IMEで使う名詞・固有名詞を収集するプロジェクトです。既存のIssue用Actionsに加え、カテゴリ別の収集データを累積ZIPで配布するRelease専用Actionsがあります。有料AI APIは使用しません。

## 2つのActions

| Actions | 用途 | 保存・配布先 |
|---|---|---|
| Daily new words | 従来のIssue収集・読み更新 | 日次Issue、`data/seen.tsv` |
| Daily word release | 読み・日本語使用例を確認し、v1.7.256の全13パックにない表記をカテゴリ別に蓄積 | `data/release/`、同じReleaseの`new-words.zip` |

両方ともToronto時間19時に実行し、手動実行にも対応します。Gitへの書き込みは同じ実行制御グループで直列化します。公開リポジトリの標準Ubuntu runnerで動作します。[GitHub Actionsの料金仕様](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

Issue側の投稿・スケジュール・既存のコード検索は従来のmainの動作を維持します。Issue #6のメタデータv3を履歴として読み込める互換対応だけ追加しています。従来のコード検索はバイナリ辞書の収録確認にはなりません。Release側の履歴はIssue側から独立し、辞書照合には公開済みの辞書ZIPを読み取ります。converterへの書き込みやビルドは行いません。

## Releaseの取得とファイル形式

[累積Release](https://github.com/KazumaProject/New-word/releases/tag/new-words)の **new-words.zip** を取得してください。初回公開後に利用できます。同じReleaseタグ`new-words`と同じZIP名を更新します。日付別Releaseは作りません。

ZIPを展開すると次のファイルが入っています。

| ファイル | 内容 |
|---|---|
| `dictionary-0001.tsv` | 読み・表記・品詞ラベルの3列。UTF-8、BOM・ヘッダーなし、タブ区切り、改行LF |
| `metadata-0001.jsonl` | TSVと同じ順番の語。分類、使用例、読みの根拠、確認日、手動メモ、辞書照合状況 |
| `manifest.json` | 形式バージョン、データのGitコミット、更新日時、列・カテゴリ定義、件数、各ファイルのサイズとSHA-256 |

TSVにはURL、日付、カテゴリ、メモを混ぜません。品詞はMozcのラベル文字列です。数値の品詞ID・変換コストは後の辞書連携で設定します。

**辞書照合はv1.7.256の全13パックを対象に行います。** [公開済みの辞書ZIP](https://github.com/KazumaProject/kotlin-kana-kanji-converter/releases/tag/v1.7.256)を1実行に1回取得し、実際の変換出力をすべて読み取ってSQLite索引を作ります。いずれかの読みで同じ表記が存在すれば除外します。NFKC・空白・英字大小の正規化は収集時と共通です。辞書バージョン・ZIPと読み出し結果のSHA-256・パックごとの件数を[data/dictionary-release.json](data/dictionary-release.json)で固定しています。欠損・破損・全件一致を検証できない場合は収集・再配布を停止します。

配布する各語の`dictionary_check.status`は`missing`、manifestでは`verified`です。確認した辞書Release、全パックの件数、チェックサム、語ごとの確認日も保存します。古い`not_checked`データは再照合してから配布します。収録済みと判定した保存語は`present`としてGitに履歴・手動メモを残し、TSV・配布メタデータから除外します。

この照合は**単語としての表記の登録**を確認します。カタカナ化や複数語の組み合わせでIMEが変換できる場合でも、同じ表記の辞書エントリがなければ残ります。辞書への追加・品詞ID・変換コスト調整は別工程です。

独自に添付するAssetはZIP 1個です。GitHubが自動表示するSource codeのZIP・tar.gzは辞書用配布ファイルではありません。

## 初期データと採用条件

初期データは[2026-10-07の確認済み10語](lists/2026-10-07.md)です。v1.7.256との実データ照合で6語が収録済みと確認できたため、配布対象は残る4語です。10語すべての確認結果と元の根拠はGitに保存しています。[元のメタデータ](data/lists/2026-10-07.json)と[Issue #6](https://github.com/KazumaProject/New-word/issues/6)も参照できます。その他の過去IssueはReleaseへ取り込みません。

- 独立した日本語使用例を2件以上、本文で確認します。同一記事や通信社の転載は1件として扱います。
- かな表記、ルビ、明示的な読み、根拠付きの確認済み資料から完全な読みを確認します。推定読み・読みの矛盾は保留します。
- 製品・サービス・作品・人名・組織・地名などには公式名称の根拠も必要です。
- 文章、宣伝文句、不完全な名称、数字だけの文字列、入力と同じひらがな表記は除外します。
- 表記をNFKC・空白・英字大小で正規化し、Release内で重複を除きます。別の読みで同じ表記を重複登録しません。
- 全13パックを照合し、いずれかの読みで同じ表記の登録がある語は採用しません。ひらがな・カタカナを返す辞書エントリも対象です。

## カテゴリと日次上限

検索語とカテゴリ順は[data/categories.json](data/categories.json)で設定します。

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

Google News RSSを分類ごとに検索し、24時間、30日、365日、期間制限なしの順に探索を広げます。新語だけでなく、以前から使われている名称・専門用語も対象です。全分類タグを保持し、設定順の最初の分類を主分類にします。分類を順番に巡回して、合計最大10語を採用します。

採用上限はTorontoの日付ごとに10語です。保留語の再確認も1日最大10語。日付付きの採用履歴・再確認履歴をGitへ保存し、同日の再実行でも上限を守ります。本文取得は1実行20ページ以内で、半分を保留再確認のために先に確保します。記事キャッシュを使用例・読み・公式根拠の確認で共有します。

## 保存・サイズ・復旧

確定データは`data/release/entries-*.jsonl`、保留候補と当日の再確認履歴は`data/release/pending.json`に保存します。手動メモは`manual_notes`に記入できます。自動収集では既存の確定語を上書きせず、保留語が採用された際もメモを保持します。根拠付きの読みは`data/readings.json`で管理します。

データを検証し、Gitへcommit・pushしてからZIPを配布します。push失敗時には配布しません。収集失敗時も保留根拠・再確認履歴の保存を試みますが、ZIPは公開しません。確定データが変わらない日は、同じZIPの置換を省略します。

ファイルは非圧縮32MiBを上限に、語の行単位で分割します。TSVとメタデータの対応を保ち、`0002`以降をmanifestに列挙します。辞書照合情報を含む初期配布4語のサイズからの概算では、毎日10語追加して年間TSV約0.3MB・メタデータ約6.2MB（非圧縮）なので、当面は各1ファイルです。32MiBはGitの50MiB警告・100MiB上限に余裕を取った設計値です。[Gitのファイル制限](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)

分割後も配布ZIP・Releaseは1つです。ZIPは2GiB未満を検証します。[Releaseの制限](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)

Asset置換は旧ファイルを先に削除するため、アップロード失敗時にZIPが一時的に取得できなくなる場合があります。Gitの累積データは残るので、次回実行または手動`rebuild`で復旧できます。[GitHub CLIの置換仕様](https://cli.github.com/manual/gh_release_upload)

## 初回公開と手動実行

PRのマージ後、Actions → **Daily word release** → Run workflow → `mode: rebuild`を選びます。保存済みの語を全13パックに照合し、ニュース収集せずにZIPを配布します。その後は日次実行または`mode: collect`で追加します。`rebuild`は内容が同じでも再アップロードします。収録済み語をすべて除外して0語になった場合も、空のTSV・メタデータと件数0のmanifestを配布できます。

Releaseは更新可能である必要があります。既存Releaseがimmutableの場合は停止し、リポジトリ設定は変更しません。新規作成時にimmutable設定の確認APIへアクセスできない場合も停止します。このAPIには管理者の読み取り権限が必要なため、その場合は管理者が設定を確認し、更新可能な`new-words` Releaseを先に作成してから`rebuild`を実行してください。既存の可変Releaseの更新は標準`GITHUB_TOKEN`の`contents: write`で行います。[immutable Release](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)、[設定確認APIの権限](https://docs.github.com/en/rest/repos/repos#check-if-immutable-releases-are-enabled-for-a-repository)

Release公開は本家リポジトリのデフォルトブランチだけで実行します。forkやPRのテストでReleaseを公開することはありません。Actionsの混雑により日次実行が遅れる場合があります。

ローカルではPython、Java 17、認証済みGitHub CLIが必要です。辞書照合索引は一時ファイルで、Git・配布ZIPに保存しません。保存済みデータからZIPを生成する場合（以下はbashの例）:

```sh
python -m pip install -r requirements.txt
export DICTIONARY_INDEX_PATH="$(mktemp -d)/dictionary.sqlite"
python scripts/dictionary_assets.py --output "$DICTIONARY_INDEX_PATH"
python scripts/collect_release_words.py --mode rebuild
python scripts/release_archive.py
```

ZIPはGit管理外の`dist/new-words.zip`に生成します。manifestのコミットは確定データ・カテゴリ・配布形式を最後に変更したコミットで、保留履歴やIssue履歴だけの更新では変化しません。

既存Issueの読みだけを更新する場合は **Daily new words** の`refresh_readings`を使用します。従来のIssue収集のコード検索には引き続き`CODE_SEARCH_TOKEN`が必要です。

## テスト

```sh
python -B -m unittest discover -s tests -v
```

Windowsでは`tzdata`をインストールし、`python -X utf8 -B`を使用できます。JavaがPATHにない場合は`DICTIONARY_JAVA`にjava実行ファイルの絶対パスを指定します。テストは外部通信・実際のIssueやRelease公開を行わず、辞書の全パック・かな出力・別の読み・破損時の停止、従来のIssue動作、v1/v3互換、カテゴリ、読み、転載、日次上限、累積データ、分割、整合性、置換失敗からの復旧を検証します。[辞書照合の実データ検証](docs/dictionary-check.md)も参照できます。
