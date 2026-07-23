# toushi-price-alerts

[chart_check.html](../tools/chart_check.html) の水平線チャートツール用の通知バックエンド。2つのスクリプトがある。

- **main.py**(5分おき): 🔔設定した価格アラート・👁ウォッチリスト(自動線引き)をチェックし、
  価格がラインを通過/接近したらGmail経由でメール通知
- **market_scan.py**(1日1回・朝6:00 JST): 日経225 + 🔔/👁登録中の株式銘柄を週足でスキャンし、
  支持線に接近している銘柄を反応回数の多い順に上位だけダイジェストメールで知らせる

データは共通してFirebase Realtime DB(`/toushi_alerts/<ALERT_KEY>`)に保存されている。
このリポジトリは公開(public)なので、鍵・パスワードはコードに書かず、
すべて **GitHub Secrets** から環境変数として読む。

## 初期セットアップ(1回だけ)

1. このリポジトリの Settings → Secrets and variables → Actions で以下の3つを登録:
   - `ALERT_KEY` — chart_check.html の `ALERT_SYNC.key` と同じ値
   - `GMAIL_USER` — 通知メールの送信元Gmailアドレス
   - `GMAIL_APP_PASSWORD` — そのGmailの[アプリパスワード](https://myaccount.google.com/apppasswords)(2段階認証が必要)
2. Actionsタブで `Check price alerts` と `Daily market scan` を一度ずつ `Run workflow`(workflow_dispatch)して、正常終了することを確認
3. 以降は自動実行される(`.github/workflows/check_price_alerts.yml` = 5分おき、`.github/workflows/market_scan.yml` = 1日1回)

## ローカルでテストする場合

```bash
pip install -r requirements.txt
ALERT_KEY=xxx GMAIL_USER=xxx GMAIL_APP_PASSWORD=xxx python main.py
ALERT_KEY=xxx GMAIL_USER=xxx GMAIL_APP_PASSWORD=xxx python market_scan.py
```

market_scan.py は日経225の225銘柄を1つずつ取得するため、ローカル実行では数分かかる。
