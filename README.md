# toushi-price-alerts

[chart_check.html](../tools/chart_check.html) の水平線チャートツールで🔔設定した価格アラートを、
GitHub Actionsで5分おきにチェックし、価格がラインを通過したらGmail経由でメール通知するスクリプト。

アラートのデータ自体はFirebase Realtime DB(`/toushi_alerts/<ALERT_KEY>`)に保存されている。
このリポジトリは公開(public)なので、鍵・パスワードはコードに書かず、
すべて **GitHub Secrets** から環境変数として読む。

## 初期セットアップ(1回だけ)

1. このリポジトリの Settings → Secrets and variables → Actions で以下の3つを登録:
   - `ALERT_KEY` — chart_check.html の `ALERT_SYNC.key` と同じ値
   - `GMAIL_USER` — 通知メールの送信元Gmailアドレス
   - `GMAIL_APP_PASSWORD` — そのGmailの[アプリパスワード](https://myaccount.google.com/apppasswords)(2段階認証が必要)
2. Actionsタブで `Check price alerts` ワークフローを一度 `Run workflow`(workflow_dispatch)して、正常終了することを確認
3. 以降は5分おきに自動実行される(`.github/workflows/check_price_alerts.yml`)

## ローカルでテストする場合

```bash
pip install -r requirements.txt
ALERT_KEY=xxx GMAIL_USER=xxx GMAIL_APP_PASSWORD=xxx python main.py
```
