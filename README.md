# New-word

日本語IME向けの新語候補を、無料のGitHub Actionsだけで毎日収集するリポジトリです。

## 動作

- Toronto時間の毎日19時に実行
- Google News RSSから新サービス・新製品・新技術などの候補を収集
- 2媒体以上で確認できた候補のみ採用
- 過去Issueと data/seen.tsv を使って重複排除
- KazumaProject/JapaneseKeyboard をGitHubコード検索して既存語を追加チェック
- 最新のMozc src/data/dictionary_oss/id.def を毎回取得
- Mozc互換の品詞文字列からIDを逆引き
- 候補がある日のみ 新語候補 YYYY-MM-DD Issueを作成/更新
- OpenAI APIなどの有料AI APIは使用しません

## 手動実行

Actions → Daily new words → Run workflow

手動実行時は時刻チェックをスキップします。

## 注意

JapaneseKeyboard の辞書の一部はバイナリアセットなので、GitHubのコード検索だけでは中身を完全検索できません。
そのため、この自動化が一度扱った候補は data/seen.tsv と過去Issueの両方で永続的に重複防止します。

Latin文字の製品名など、公式の読みを機械的に保証できない語は「要確認」とし、読みを捏造しません。
