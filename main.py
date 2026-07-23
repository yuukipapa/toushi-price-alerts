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
import io
import math
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import mplfinance as mpf
import pandas as pd
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


# ── チャート画像の生成(通知メールに添付する用) ──

def asset_symbol(entry: dict) -> str:
    # GitHub Actionsのランナーには日本語フォントが無く、チャート画像内のタイトルに
    # 日本語ラベルを使うと文字が表示されない(tofu化する)ため、ASCIIのティッカーを使う
    return entry.get("ysym") or entry.get("sym") or entry.get("isin") or entry.get("assetKey") or "?"



def render_chart_png(candles: list, title: str, hline: float = None, aline: tuple = None, n: int = 60) -> bytes:
    rows = candles[-n:]
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"], unit="ms")
    df = df.set_index("t").rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close"})

    kwargs = {}
    if hline is not None:
        kwargs["hlines"] = dict(hlines=[hline], colors=["#2962ff"], linestyle="--", linewidths=[1.2])
    if aline is not None:
        # mplfinanceのalinesは「表示中のローソク足の日付」と厳密一致する点しか受け付けないため、
        # 直線の傾きはそのまま保ちつつ、両端を表示範囲内の実在する日付にスナップする
        (t1, p1), (t2, p2) = aline
        visible_ts = [r["t"] for r in rows]
        slope = (math.log(p2) - math.log(p1)) / (t2 - t1) if t2 != t1 else 0.0

        def price_at(t_ms):
            return math.exp(math.log(p1) + slope * (t_ms - t1))

        t1s = min(visible_ts, key=lambda x: abs(x - t1))
        t2s = min(visible_ts, key=lambda x: abs(x - t2))
        pts = [(pd.to_datetime(t1s, unit="ms"), price_at(t1s)), (pd.to_datetime(t2s, unit="ms"), price_at(t2s))]
        kwargs["alines"] = dict(alines=[pts], colors=["#2962ff"], linewidths=[1.2])

    buf = io.BytesIO()
    mpf.plot(
        df, type="candle", style="yahoo", title=title, volume=False,
        figsize=(7, 4), savefig=dict(fname=buf, dpi=110, bbox_inches="tight"),
        **kwargs,
    )
    buf.seek(0)
    return buf.getvalue()


# ── メール送信 ──

def send_email(gmail_user: str, gmail_pass: str, to_addr: str, subject: str, body: str, images: list = None) -> None:
    if not images:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = gmail_user
        msg["To"] = to_addr
    else:
        msg = MIMEMultipart("related")
        msg["Subject"] = subject
        msg["From"] = gmail_user
        msg["To"] = to_addr

        html_body = body.replace("\n", "<br>\n")
        for img in images:
            caption = img.get("caption", "")
            html_body += f'<br>{caption}<br><img src="cid:{img["cid"]}" style="max-width:640px"><br>\n'

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain"))
        alt.attach(MIMEText(html_body, "html"))
        msg.attach(alt)

        for img in images:
            mime_img = MIMEImage(img["data"])
            mime_img.add_header("Content-ID", f"<{img['cid']}>")
            mime_img.add_header("Content-Disposition", "inline", filename=f"{img['cid']}.png")
            msg.attach(mime_img)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, [to_addr], msg.as_string())


# ── 通知文の生成(水平線トレードの考え方に沿った根拠を書く) ──

def describe_cross(direction: str, is_trend: bool, note: str) -> str:
    line_type = "斜め線(トレンドライン)" if is_trend else "水平線"
    if direction == "up":  # 下から上に抜けた(ブレイクアウト/レジサポ転換の入口)
        text = (
            f"この{line_type}を下から上に抜けました。教材の考え方では、抵抗として機能していた線を実体で上に抜けると"
            "「レジサポ転換」(それまでの抵抗がサポートに変わる)が起きたかどうかがポイントになります。"
            "抜けた直後に飛びつくより、いったんこの線まで戻ってきてサポートとして機能するか確認してから入る方が、"
            "損切りが浅く・リスクリワードが合いやすいとされています。ヒゲだけでなく実体で維持できているかも確認してください。"
        )
    else:  # 上から下に割れた(損切り・シナリオ崩れの合図)
        text = (
            f"この{line_type}を上から下に割りました。教材の損切りの基準は「シナリオが崩れたら」であり、"
            "支持していた線を割ったことはまさにそのシナリオ崩れのサインです。実体で割れているか(ヒゲだけでないか)を確認し、"
            "実体で割れていれば早めに見切るのが基本とされています。最底辺の線まで割ったならその銘柄を一旦手放す、という考え方も教材にあります。"
        )
    if note:
        text += f"\n\nこの線を引いたときのメモ: {note}"
    return text


def describe_level_context(current: float, level_price: float, touches: int, kind: str) -> str:
    if kind == "ath":
        return (
            "現在価格が上場来高値(ATH)圏にあります。教材の考え方では、ATH更新が続くのは上昇トレンドとして正常な状態ですが、"
            "その後BOX高値まで戻れない/高値で反発して落ちる場合は下落トレンド突入のシグナルとされています。"
            "更新が続くか、それとも失速するかを実体で確認してください。"
        )
    is_support = level_price <= current
    dist_pct = abs(current - level_price) / level_price * 100 if level_price else 0
    if is_support:
        return (
            f"現在価格は、週足で{touches}回反応している支持線({level_price:.4g})の{dist_pct:.1f}%上にいます。"
            "反応回数が多い線ほど効きやすい、というのが教材の考え方です。ここで下げ止まりを実体で確認できれば、"
            "「サポートタッチでの買い場」の候補になります(ブレイク後に追いかけるより損切りが浅く、利益が大きくなりやすいとされる位置)。"
            "逆に実体でこの線を割ってしまった場合は、シナリオが崩れたとみなして早めに見切るのが基本です。"
        )
    else:
        return (
            f"現在価格は、週足で{touches}回反応している抵抗線({level_price:.4g})の{dist_pct:.1f}%下にいます。"
            "教材ではこの位置は「抵抗線に当たって落ちることが想定されるタイミング」であり、新規で買うよりも利確を検討する場面とされています。"
            "実体でこの線を超えて維持できれば「レジサポ転換」の可能性もあるので、超えた後の値動きも確認してください。"
        )


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

        is_trend = a.get("kind") == "trend"
        try:
            line_price = trend_price_now(a) if is_trend else a["price"]
        except Exception as e:
            print(f"[alerts] trend extrapolation failed for {a.get('label')}: {e}")
            continue
        line_desc = f"斜め線(延長線上の現在値 {line_price:.4g})" if is_trend else f"{line_price}"

        side = "above" if price >= line_price else "below"
        if a.get("lastSide") is None:
            a["lastSide"] = side  # 初回チェックは基準の記録のみ(いきなり通知しない)
            changed = True
            print(f"[alerts] baseline set: {a['label']} {line_desc} (current {price}, {side})")
            continue
        if side == a["lastSide"]:
            print(f"[alerts] no change: {a['label']} {line_desc} (current {price}, still {side})")
        if side != a["lastSide"]:
            a["lastSide"] = side
            a["firedAt"] = datetime.now(timezone.utc).isoformat()
            changed = True
            if to_addr:
                try:
                    subject = f"🔔 価格アラート: {a['label']} が {line_desc} を通過"
                    direction = "up" if side == "above" else "down"
                    reason = describe_cross(direction, is_trend, a.get("note") or "")
                    body = (
                        f"{a['label']} の価格が、設定していたライン {line_desc} を通過しました。\n\n"
                        f"現在価格: {price}\n\n"
                        f"【この通知の根拠】\n{reason}\n\n"
                        "チャートツールで確認: https://wyujiro-toushi-chart.web.app\n\n"
                        "※ これは投資助言ではありません。売買の最終判断は自分で行ってください。"
                    )
                    images = _render_alert_chart(a, is_trend, line_price)
                    send_email(gmail_user, gmail_pass, to_addr, subject, body, images=images)
                    print(f"[alerts] sent: {a['label']} {line_desc}")
                except Exception as e:
                    print(f"[alerts] email send failed: {e}")
            else:
                print("[alerts] notifyEmail not set, skipping email")

    return changed


def _render_alert_chart(a: dict, is_trend: bool, line_price: float) -> list:
    # 通知するときだけ過去足を取得してチャート画像を作る(毎回のチェックでは取得しない)
    try:
        if a.get("assetType") == "stock":
            candles = fetch_stock_candles(a["ysym"])
        elif a.get("assetType") == "fund":
            candles = fetch_fund_candles(a["isin"], a["assoc"])
        elif a.get("assetType") == "crypto":
            candles = fetch_crypto_candles(a["sym"])
        else:
            return None
        if is_trend:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            aline = ((a["t1"], a["p1"]), (now_ms, line_price))
            png = render_chart_png(candles, asset_symbol(a), aline=aline)
        else:
            png = render_chart_png(candles, asset_symbol(a), hline=line_price)
        return [{"cid": "chart1", "data": png, "caption": ""}]
    except Exception as e:
        print(f"[alerts] chart render failed: {e}")
        return None


def trend_price_now(a: dict) -> float:
    # 斜め線(トレンドライン)は時間で価格が変わるため、保存した2点(t1,p1)-(t2,p2)から
    # 現在時刻での延長線上の価格をlog空間で線形補間して求める(chart_check.htmlの傾き計算と同じ考え方)
    t1, p1, t2, p2 = a["t1"], a["p1"], a["t2"], a["p2"]
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    if t2 == t1:
        return p2
    slope = (math.log(p2) - math.log(p1)) / (t2 - t1)
    return math.exp(math.log(p1) + slope * (now_ms - t1))


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
                    reason = describe_level_context(current, lv["price"], lv["touches"], lv["kind"])
                    body = (
                        f"{w['label']} の価格が、自動検出した{kind_label} {lv['price']:.4g} の"
                        f"±{NEAR_PCT * 100:.0f}%以内に近づきました。\n\n"
                        f"現在価格: {current}\n\n"
                        f"【この通知の根拠】\n{reason}\n\n"
                        "チャートツールで確認: https://wyujiro-toushi-chart.web.app\n\n"
                        "※ これは投資助言ではありません。売買の最終判断は自分で行ってください。"
                    )
                    try:
                        png = render_chart_png(candles, asset_symbol(w), hline=lv["price"])
                        images = [{"cid": "chart1", "data": png, "caption": ""}]
                    except Exception as e:
                        print(f"[watchlist] chart render failed: {e}")
                        images = None
                    send_email(gmail_user, gmail_pass, to_addr, subject, body, images=images)
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
