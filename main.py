"""
価格アラート監視(GitHub Actionsから定期実行するスクリプト)

chart_check.html で🔔設定した水平線アラートを見て、価格がその線を通過したら
Gmail経由でメール通知する。データは Firebase Realtime DB の
/toushi_alerts/<ALERT_KEY> に chart_check.html 側の ALERT_SYNC と同じ形式で保存されている。

このリポジトリは公開(public)前提なので、鍵・パスワードはコードに書かず
すべて環境変数(GitHub Secrets経由)から読む。
必要な環境変数: ALERT_KEY, GMAIL_USER, GMAIL_APP_PASSWORD
"""
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText

import requests
from curl_cffi import requests as cffi_requests

DB_URL = "https://routine-sync-7029e-default-rtdb.asia-southeast1.firebasedatabase.app"


def fetch_stock_price(ysym: str) -> float:
    # Yahoo は python/curl のTLS指紋だと429で弾くことがあるため、Chromeを偽装して取得する
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?range=1d&interval=1d"
    r = cffi_requests.get(url, impersonate="chrome124", timeout=10)
    r.raise_for_status()
    result = r.json()["chart"]["result"][0]
    price = result["meta"].get("regularMarketPrice")
    if price is None:
        raise ValueError("no regularMarketPrice in response")
    return float(price)


def fetch_fund_price(isin: str, assoc: str) -> float:
    # 投信協会CSV(設定来の日次基準価額・先頭行=最新日)
    url = (
        "https://toushin-lib.fwg.ne.jp/FdsWeb/FDST030000/csv-file-download"
        f"?isinCd={isin}&associFundCd={assoc}"
    )
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    text = r.content.decode("shift_jis", errors="ignore")
    for line in text.splitlines():
        m = re.match(r"^(\d{4})年(\d{2})月(\d{2})日,(\d+(?:\.\d+)?)", line)
        if m:
            return float(m.group(4))
    raise ValueError("no fund price row found")


def fetch_crypto_price(sym: str) -> float:
    # Binanceは米国リージョンのIPを451でブロックするため(GitHub ActionsのランナーもUS)、
    # chart_check.html側(ブラウザから直接Binance)とは別にCoinbaseの公開APIを使う(キー不要)
    base = sym[:-4] if sym.endswith("USDT") else sym
    url = f"https://api.coinbase.com/v2/prices/{base}-USD/spot"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return float(r.json()["data"]["amount"])


def send_alert_email(gmail_user: str, gmail_pass: str, to_addr: str, alert: dict, price) -> None:
    subject = f"🔔 価格アラート: {alert['label']} が {alert['price']} を通過"
    body = (
        f"{alert['label']} の価格が、設定していたライン {alert['price']} を通過しました。\n\n"
        f"現在価格: {price}\n"
        f"メモ: {alert.get('note') or '(なし)'}\n\n"
        "チャートツールで確認: 10_toushi/tools/chart_check.html\n\n"
        "※ これは投資助言ではありません。売買の最終判断は自分で行ってください。"
    )
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_addr
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, [to_addr], msg.as_string())


def check_price_alerts() -> None:
    alert_key = os.environ["ALERT_KEY"]
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]
    alerts_url = f"{DB_URL}/toushi_alerts/{alert_key}.json"

    res = requests.get(alerts_url, timeout=10)
    res.raise_for_status()
    doc = res.json() or {}
    alerts = doc.get("alerts") or []
    active = [a for a in alerts if a.get("active")]
    if not active:
        print("no active alerts")
        return

    to_addr = doc.get("notifyEmail")
    price_cache: dict = {}
    changed = False

    for a in active:
        cache_key = (
            a.get("assetType"),
            a.get("ysym") or a.get("sym") or f"{a.get('isin')}|{a.get('assoc')}",
        )
        if cache_key not in price_cache:
            try:
                if a.get("assetType") == "stock":
                    price_cache[cache_key] = fetch_stock_price(a["ysym"])
                elif a.get("assetType") == "fund":
                    price_cache[cache_key] = fetch_fund_price(a["isin"], a["assoc"])
                elif a.get("assetType") == "crypto":
                    price_cache[cache_key] = fetch_crypto_price(a["sym"])
                else:
                    price_cache[cache_key] = None
            except Exception as e:  # 個別銘柄の取得失敗は次回リトライに任せる
                print(f"price fetch failed for {cache_key}: {e}")
                price_cache[cache_key] = None

        price = price_cache[cache_key]
        if price is None:
            continue

        side = "above" if price >= a["price"] else "below"
        if a.get("lastSide") is None:
            # 初回チェックは基準の記録のみ(いきなり通知しない)
            a["lastSide"] = side
            changed = True
            continue
        if side != a["lastSide"]:
            a["lastSide"] = side
            a["firedAt"] = datetime.now(timezone.utc).isoformat()
            changed = True
            if to_addr:
                try:
                    send_alert_email(gmail_user, gmail_pass, to_addr, a, price)
                    print(f"sent alert email: {a['label']} @ {a['price']}")
                except Exception as e:
                    print(f"email send failed: {e}")
            else:
                print("notifyEmail not set, skipping email")

    if changed:
        requests.put(alerts_url, json={**doc, "alerts": alerts}, timeout=10)


if __name__ == "__main__":
    try:
        check_price_alerts()
    except Exception as e:
        print(f"check_price_alerts failed: {e}")
        sys.exit(1)
