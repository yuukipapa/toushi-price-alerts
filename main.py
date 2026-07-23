"""
価格アラート監視(GitHub Actionsから定期実行するスクリプト)

2つの通知源をチェックする:
1. アラート(chart_check.html で🔔設定した水平線) — 価格が線を通過したら通知
2. ウォッチリスト(👁登録した銘柄) — 週足で自動的に線を引き直し、価格がその線の±1%に
   近づいたら通知(手動で線を引かなくていい版)

データは Firebase Realtime DB の /toushi_alerts/<ALERT_KEY> に
chart_check.html 側の ALERT_SYNC と同じ形式で保存されている。

このリポジトリは公開(public)前提なので、鍵・パスワードはコードに書かず
すべて環境変数(GitHub Secrets経由)から読む。
必要な環境変数: ALERT_KEY, GMAIL_USER, GMAIL_APP_PASSWORD
"""
import math
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText

import requests
from curl_cffi import requests as cffi_requests

DB_URL = "https://routine-sync-7029e-default-rtdb.asia-southeast1.firebasedatabase.app"
NEAR_PCT = 0.01       # ウォッチリスト: 線の±1%に近づいたら通知
COOLDOWN_HOURS = 24    # 同じ線について再通知するまでの間隔


# ── 価格取得(現在値) ──

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
    # 投信協会CSV(設定来の日次基準価額。日付は古い順に並んでいるので末尾行=最新日)
    url = (
        "https://toushin-lib.fwg.ne.jp/FdsWeb/FDST030000/csv-file-download"
        f"?isinCd={isin}&associFundCd={assoc}"
    )
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    text = r.content.decode("shift_jis", errors="ignore")
    price = None
    for line in text.splitlines():
        m = re.match(r"^(\d{4})年(\d{2})月(\d{2})日,(\d+(?:\.\d+)?)", line)
        if m:
            price = float(m.group(4))
    if price is None:
        raise ValueError("no fund price row found")
    return price


def fetch_crypto_price(sym: str) -> float:
    # Binanceは米国リージョンのIPを451でブロックするため(GitHub ActionsのランナーもUS)、
    # chart_check.html側(ブラウザから直接Binance)とは別にCoinbaseの公開APIを使う(キー不要)
    base = sym[:-4] if sym.endswith("USDT") else sym
    url = f"https://api.coinbase.com/v2/prices/{base}-USD/spot"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return float(r.json()["data"]["amount"])


# ── 過去足の取得(ウォッチリストの自動線引き用・週足) ──

def fetch_stock_candles(ysym: str) -> list:
    # 全期間(上場来)だと株式分割前の古い安値がノイズとして支持線に混ざるため、直近8年に絞る
    period1 = int(datetime.now(timezone.utc).timestamp()) - 8 * 365 * 86400
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}"
        f"?period1={period1}&period2=9999999999&interval=1wk"
    )
    r = cffi_requests.get(url, impersonate="chrome124", timeout=15)
    r.raise_for_status()
    result = r.json()["chart"]["result"][0]
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    candles = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        candles.append({"t": t * 1000, "o": o, "h": h, "l": l, "c": c})
    return candles


def aggregate_weekly(daily: list) -> list:
    # daily: [(datetime, price)] を日付昇順で受け取り、ISO週ごとに集計する
    weeks = {}
    for dt, price in daily:
        key = dt.isocalendar()[:2]
        w = weeks.get(key)
        if w is None:
            weeks[key] = {"t": int(dt.timestamp() * 1000), "o": price, "h": price, "l": price, "c": price}
        else:
            w["h"] = max(w["h"], price)
            w["l"] = min(w["l"], price)
            w["c"] = price
            w["t"] = int(dt.timestamp() * 1000)
    return [weeks[k] for k in sorted(weeks.keys())]


def fetch_fund_candles(isin: str, assoc: str) -> list:
    url = (
        "https://toushin-lib.fwg.ne.jp/FdsWeb/FDST030000/csv-file-download"
        f"?isinCd={isin}&associFundCd={assoc}"
    )
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    text = r.content.decode("shift_jis", errors="ignore")
    daily = []
    for line in text.splitlines():
        m = re.match(r"^(\d{4})年(\d{2})月(\d{2})日,(\d+(?:\.\d+)?)", line)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            daily.append((datetime(y, mo, d), float(m.group(4))))
    daily.sort(key=lambda x: x[0])
    return aggregate_weekly(daily)


def fetch_crypto_candles(sym: str) -> list:
    # Coinbase Exchangeの日足(直近300本≒10ヶ月)を週足に集計する
    base = sym[:-4] if sym.endswith("USDT") else sym
    url = f"https://api.exchange.coinbase.com/products/{base}-USD/candles?granularity=86400"
    r = requests.get(url, timeout=15, headers={"User-Agent": "toushi-price-alerts"})
    r.raise_for_status()
    rows = r.json()  # [[time, low, high, open, close, volume], ...] 新しい順
    daily = [
        (datetime.fromtimestamp(row[0], tz=timezone.utc).replace(tzinfo=None), row[4])
        for row in rows
    ]
    daily.sort(key=lambda x: x[0])
    return aggregate_weekly(daily)


# ── スイングハイロー検出 + クラスタリング(chart_check.html drawLines() のPython移植) ──

def detect_levels(candles: list, k: int = 2) -> list:
    n = len(candles)
    if n < 30:
        return []

    pivots = []
    for i in range(k, n - k):
        is_h, is_l = True, True
        for j in range(i - k, i + k + 1):
            if j == i:
                continue
            if candles[j]["h"] >= candles[i]["h"]:
                is_h = False
            if candles[j]["l"] <= candles[i]["l"]:
                is_l = False
        if is_h:
            pivots.append({"p": candles[i]["h"]})
        if is_l:
            pivots.append({"p": candles[i]["l"]})

    tol = 0.025
    clusters = []
    for pv in sorted(pivots, key=lambda x: x["p"]):
        match = next((cl for cl in clusters if abs(math.log(pv["p"]) - math.log(cl["center"])) < tol), None)
        if match:
            match["members"].append(pv)
            match["center"] = sum(m["p"] for m in match["members"]) / len(match["members"])
        else:
            clusters.append({"center": pv["p"], "members": [pv]})

    ath = max(c["h"] for c in candles)
    last_p = candles[-1]["c"]
    strong = [
        {"price": cl["center"], "touches": len(cl["members"]), "kind": "lv"}
        for cl in clusters if len(cl["members"]) >= 2
    ]
    above = sorted([lv for lv in strong if lv["price"] > last_p], key=lambda lv: -lv["touches"])[:4]
    below = sorted([lv for lv in strong if lv["price"] <= last_p], key=lambda lv: -lv["touches"])[:4]
    levels = above + below

    ath_match = next((lv for lv in levels if abs(math.log(lv["price"]) - math.log(ath)) < tol), None)
    if ath_match:
        ath_match["kind"] = "ath"
    else:
        levels.append({"price": ath, "touches": 1, "kind": "ath"})
    return levels


# ── メール送信 ──

def send_email(gmail_user: str, gmail_pass: str, to_addr: str, subject: str, body: str) -> None:
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_addr
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, [to_addr], msg.as_string())


# ── ①アラート(手動で🔔設定した線) ──

def check_alerts(doc: dict, gmail_user: str, gmail_pass: str) -> bool:
    alerts = doc.get("alerts") or []
    active = [a for a in alerts if a.get("active")]
    if not active:
        return False

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
                print(f"[alerts] price fetch failed for {cache_key}: {e}")
                price_cache[cache_key] = None

        price = price_cache[cache_key]
        if price is None:
            continue

        side = "above" if price >= a["price"] else "below"
        if a.get("lastSide") is None:
            a["lastSide"] = side  # 初回チェックは基準の記録のみ(いきなり通知しない)
            changed = True
            print(f"[alerts] baseline set: {a['label']} @ {a['price']} (current {price}, {side})")
            continue
        if side == a["lastSide"]:
            print(f"[alerts] no change: {a['label']} @ {a['price']} (current {price}, still {side})")
        if side != a["lastSide"]:
            a["lastSide"] = side
            a["firedAt"] = datetime.now(timezone.utc).isoformat()
            changed = True
            if to_addr:
                try:
                    subject = f"🔔 価格アラート: {a['label']} が {a['price']} を通過"
                    body = (
                        f"{a['label']} の価格が、設定していたライン {a['price']} を通過しました。\n\n"
                        f"現在価格: {price}\n"
                        f"メモ: {a.get('note') or '(なし)'}\n\n"
                        "チャートツールで確認: https://wyujiro-toushi-chart.web.app\n\n"
                        "※ これは投資助言ではありません。売買の最終判断は自分で行ってください。"
                    )
                    send_email(gmail_user, gmail_pass, to_addr, subject, body)
                    print(f"[alerts] sent: {a['label']} @ {a['price']}")
                except Exception as e:
                    print(f"[alerts] email send failed: {e}")
            else:
                print("[alerts] notifyEmail not set, skipping email")

    return changed


# ── ②ウォッチリスト(自動で線を引いて監視) ──

def level_key(price: float) -> str:
    return f"{price:.3g}"


def check_watchlist(doc: dict, gmail_user: str, gmail_pass: str) -> bool:
    watchlist = doc.get("watchlist") or []
    if not watchlist:
        return False

    to_addr = doc.get("notifyEmail")
    now = datetime.now(timezone.utc)
    changed = False

    for w in watchlist:
        try:
            if w.get("assetType") == "stock":
                candles = fetch_stock_candles(w["ysym"])
                current = fetch_stock_price(w["ysym"])
            elif w.get("assetType") == "fund":
                candles = fetch_fund_candles(w["isin"], w["assoc"])
                current = fetch_fund_price(w["isin"], w["assoc"])
            elif w.get("assetType") == "crypto":
                candles = fetch_crypto_candles(w["sym"])
                current = fetch_crypto_price(w["sym"])
            else:
                continue
        except Exception as e:
            print(f"[watchlist] fetch failed for {w.get('label')}: {e}")
            continue

        levels = detect_levels(candles)
        if not levels:
            print(f"[watchlist] {w['label']}: not enough candles to detect levels ({len(candles)})")
            continue

        nearest = min(levels, key=lambda lv: abs(current - lv["price"]) / lv["price"] if lv["price"] > 0 else float("inf"))
        nearest_dist = abs(current - nearest["price"]) / nearest["price"] * 100 if nearest["price"] > 0 else float("inf")
        print(f"[watchlist] {w['label']}: current={current} nearest_level={nearest['price']:.4g} ({nearest_dist:.1f}% away)")

        notified = w.get("notifiedLevels") or {}
        for lv in levels:
            if lv["price"] <= 0:
                continue
            dist_pct = abs(current - lv["price"]) / lv["price"]
            if dist_pct > NEAR_PCT:
                continue

            key = level_key(lv["price"])
            last_notified = notified.get(key)
            if last_notified:
                try:
                    elapsed_h = (now - datetime.fromisoformat(last_notified)).total_seconds() / 3600
                    if elapsed_h < COOLDOWN_HOURS:
                        continue
                except ValueError:
                    pass

            if to_addr:
                try:
                    kind_label = {"ath": "上場来高値(ATH)", "lv": f"{lv['touches']}回反応の水平線"}.get(lv["kind"], "水平線")
                    subject = f"👁 ウォッチ通知: {w['label']} が {kind_label}({lv['price']:.4g})に接近"
                    body = (
                        f"{w['label']} の価格が、自動検出した{kind_label} {lv['price']:.4g} の"
                        f"±{NEAR_PCT * 100:.0f}%以内に近づきました。\n\n"
                        f"現在価格: {current}\n"
                        f"検出した線: {lv['price']:.4g}({kind_label})\n\n"
                        "チャートツールで確認: https://wyujiro-toushi-chart.web.app\n\n"
                        "※ これは投資助言ではありません。売買の最終判断は自分で行ってください。"
                    )
                    send_email(gmail_user, gmail_pass, to_addr, subject, body)
                    print(f"[watchlist] sent: {w['label']} @ {lv['price']:.4g} (current {current})")
                except Exception as e:
                    print(f"[watchlist] email send failed: {e}")

            notified[key] = now.isoformat()
            changed = True

        # 古い通知履歴が無限に増えないよう、直近30件だけ残す
        if len(notified) > 30:
            for k in sorted(notified, key=lambda k: notified[k])[: len(notified) - 30]:
                del notified[k]
            changed = True
        w["notifiedLevels"] = notified

    return changed


def main() -> None:
    alert_key = os.environ["ALERT_KEY"]
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]
    doc_url = f"{DB_URL}/toushi_alerts/{alert_key}.json"

    res = requests.get(doc_url, timeout=10)
    res.raise_for_status()
    doc = res.json() or {}

    changed_a = check_alerts(doc, gmail_user, gmail_pass)
    changed_w = check_watchlist(doc, gmail_user, gmail_pass)

    if not (changed_a or changed_w):
        print("no changes")
        return

    requests.put(doc_url, json=doc, timeout=10)
    print("saved changes")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"main failed: {e}")
        sys.exit(1)
