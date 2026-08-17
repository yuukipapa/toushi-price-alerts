"""
日次マーケットスキャン(1日1回、GitHub Actionsで実行)

対象: 日経225 + chart_check.html で🔔/👁登録している株式銘柄。
週足で自動的に支持線・抵抗線・斜めのトレンドラインを検出し(chart_check.html「自動で線を引く」と同じロジック)、
現在価格が支持線(水平線・斜め線どちらも)に近づいている(=反発すればチャンスになりうる)銘柄を、
反応回数が多い順に上位だけメールでまとめて知らせる。

「反応回数が多い線ほど効く」というスクショ由来の考え方をそのままスコアにしているだけで、
AIによる勝率判定ではない。あくまで学習メモの考え方に沿った機械的な絞り込み。

これとは別に、confluence_scan.py(主要指数・仮想通貨11銘柄専用)と同じ「トレンドラインと水平線が
両方交わっている」判定も、この227銘柄すべてに対して行う。水平線・トレンドラインを別々にチェックする
上記の緩い条件より強いシグナルなので、該当銘柄はメール・履歴の先頭に別枠で表示する。

必要な環境変数: ALERT_KEY, GMAIL_USER, GMAIL_APP_PASSWORD
"""
import os
import time
from urllib.parse import quote

import requests

from confluence_scan import build_reason as build_confluence_reason
from confluence_scan import find_confluence_in_candles
from main import (
    DB_URL, body_wick_note, describe_level_context, describe_trendline_context, detect_levels,
    detect_trendlines, fetch_stock_candles, jst_today_str, push_scan_history,
    render_chart_png, rsi, send_digest_email, trendline_price_at,
)
from nikkei225 import NIKKEI225

NEAR_PCT = 0.02       # 支持線の±2%以内を「接近中」とみなす(日次スキャンなので🔔より少し広め)
MIN_TOUCHES = 2        # detect_levels() の強い線フィルタと同じ基準
TOP_N = 8               # メールに載せる件数
REQUEST_DELAY_SEC = 0.5  # Yahoo側のレート制限を避けるための間隔
CHART_TOOL_URL = "https://wyujiro-toushi-chart.web.app"
RSI_PERIOD = 14        # 週足RSI
RSI_OVERSOLD = 30      # これを下回ったら「売られすぎ」として通知


def chart_link(ysym: str, label: str, hline: float = None, aline: list = None) -> str:
    """その銘柄の最新チャートを開くリンク。メール送信時点でなく、クリックした時点の
    最新データを表示する(過去メールから開いても最新状態を確認できる)。

    hline/aline(メールの通知に実際に使った線の値)を渡すと、Web版はその線をそのまま再現する。
    渡さない場合、Web版は自身の自動検出(drawLines())で線を引き直すが、これはこのファイルの
    交点判定・detect_levels/detect_trendlinesとは別のアルゴリズムなので違う線になりうる。"""
    url = f"{CHART_TOOL_URL}/?ysym={quote(ysym)}&label={quote(label)}&tf=1w"
    if hline is not None:
        url += f"&hline={hline}"
    if aline is not None:
        (t1, p1), (t2, p2) = aline
        url += f"&aline={t1},{p1},{t2},{p2}"
    return url


def build_universe(doc: dict) -> list:
    universe = {ysym: name for ysym, name in NIKKEI225}
    for a in (doc.get("alerts") or []):
        if a.get("assetType") == "stock" and a.get("ysym"):
            universe.setdefault(a["ysym"], a.get("label") or a["ysym"])
    for w in (doc.get("watchlist") or []):
        if w.get("assetType") == "stock" and w.get("ysym"):
            universe.setdefault(w["ysym"], w.get("label") or w["ysym"])
    return list(universe.items())


def find_near_support(ysym: str, label: str) -> tuple[list, dict | None, dict | None]:
    candles = fetch_stock_candles(ysym)
    if len(candles) < 30:
        return [], None, None
    current = candles[-1]["c"]

    confluence = find_confluence_in_candles(candles, current)
    if confluence:
        confluence["entry"] = {"assetType": "stock", "ysym": ysym, "label": label}

    oversold = None
    rsi_value = rsi(candles, RSI_PERIOD)
    if rsi_value is not None and rsi_value < RSI_OVERSOLD:
        oversold = {"ysym": ysym, "label": label, "current": current, "rsi": rsi_value, "candles": candles}

    hits = []
    for lv in detect_levels(candles):
        if lv["price"] <= 0 or lv["price"] > current:
            continue  # 支持線(現在値より下)だけを対象にする
        if lv["touches"] < MIN_TOUCHES:
            continue
        dist_pct = (current - lv["price"]) / lv["price"]
        if dist_pct <= NEAR_PCT:
            hits.append({
                "line_type": "h", "ysym": ysym, "label": label, "current": current,
                "price": lv["price"], "touches": lv["touches"],
                "kind": lv["kind"], "dist_pct": dist_pct, "candles": candles,
            })

    # 斜めの支持線(安値同士を結んだトレンドライン)も同様にチェックする
    for tl in detect_trendlines(candles):
        if tl["kind"] != "sup":
            continue  # マーケットスキャンは「反発すればチャンス」に絞るため支持線のみ
        line_now = trendline_price_at(tl, len(candles) - 1)
        if line_now <= 0 or line_now > current:
            continue
        dist_pct = (current - line_now) / line_now
        if dist_pct <= NEAR_PCT:
            hits.append({
                "line_type": "trend", "ysym": ysym, "label": label, "current": current,
                "price": line_now, "touches": tl["touches"], "kind": "sup",
                "dist_pct": dist_pct, "candles": candles, "i1": tl["i1"], "p1": tl["p1"],
            })
    return hits, confluence, oversold


def build_reason(hit: dict) -> str:
    if hit["line_type"] == "trend":
        reason = describe_trendline_context(hit["current"], hit["price"], hit["touches"], hit["kind"])
    else:
        reason = describe_level_context(hit["current"], hit["price"], hit["touches"], hit["kind"])
    bw = body_wick_note(hit["candles"], hit["price"])
    return reason + ("\n  " + bw if bw else "")


def build_rsi_reason(hit: dict) -> str:
    return (
        f"週足RSI({RSI_PERIOD})が{hit['rsi']:.1f}まで低下し、{RSI_OVERSOLD}を割り込んでいます"
        f"(現在値 約{hit['current']:.4g})。売られすぎの水準ですが、下落が続いたまま"
        "RSIが低いまま張り付くこともあるため、これ単体を買いシグナルとはせず、"
        "支持線での反発など他の根拠と合わせて判断するのが基本です。"
    )


def main() -> None:
    alert_key = os.environ["ALERT_KEY"]
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]
    doc_url = f"{DB_URL}/toushi_alerts/{alert_key}.json"

    res = requests.get(doc_url, timeout=10)
    res.raise_for_status()
    doc = res.json() or {}
    to_addr = doc.get("notifyEmail")

    universe = build_universe(doc)
    print(f"scanning {len(universe)} symbols")

    all_hits = []
    confluence_hits = []
    rsi_hits = []
    for i, (ysym, label) in enumerate(universe):
        try:
            hits, confluence, oversold = find_near_support(ysym, label)
        except Exception as e:
            print(f"[scan] retry after failure for {label} ({ysym}): {e}")
            time.sleep(3)
            try:
                hits, confluence, oversold = find_near_support(ysym, label)
            except Exception as e2:
                print(f"[scan] failed for {label} ({ysym}): {e2}")
                hits, confluence, oversold = [], None, None
        all_hits.extend(hits)
        if confluence:
            confluence_hits.append(confluence)
            print(f"[confluence] {label}")
        if oversold:
            rsi_hits.append(oversold)
            print(f"[rsi<{RSI_OVERSOLD}] {label} rsi={oversold['rsi']:.1f}")
        if (i + 1) % 25 == 0:
            print(f"[scan] progress {i + 1}/{len(universe)}")
        time.sleep(REQUEST_DELAY_SEC)

    confluence_hits.sort(key=lambda h: h["lines_gap"] + h["now_gap"])
    rsi_hits.sort(key=lambda h: h["rsi"])
    # トレンドライン×水平線の交点として別枠で出す銘柄は、下の「支持線に接近」リストからは除外する
    # (同じ銘柄が2回、別々の見出しで並ぶのを防ぐ)
    confluence_ysyms = {h["entry"]["ysym"] for h in confluence_hits}
    all_hits = [h for h in all_hits if h["ysym"] not in confluence_ysyms]

    all_hits.sort(key=lambda h: (-h["touches"], h["dist_pct"]))
    top = all_hits[:TOP_N]
    print(
        f"found {len(confluence_hits)} confluence, {len(rsi_hits)} rsi<{RSI_OVERSOLD}, "
        f"{len(all_hits)} support candidates, sending {len(confluence_hits)} confluence + "
        f"{len(rsi_hits)} rsi + top {len(top)}"
    )

    items = []
    if not confluence_hits and not rsi_hits and not top:
        print("no candidates today, skipping email")
    else:
        intro_lines = ["今日の週足スキャン結果です。"]
        if confluence_hits:
            intro_lines.append("🎯 トレンドラインと水平線が交わる付近まで下げてきている銘柄(最有力セットアップ)、")
        if rsi_hits:
            intro_lines.append(f"📉 週足RSIが{RSI_OVERSOLD}を割り込んだ銘柄(売られすぎ)、")
        intro_lines.append("反応回数が多い順の支持線接近銘柄、の順に並んでいます。")
        intro_lines.append("※ 学習メモの考え方に沿った機械的な抽出であり、投資助言ではありません。")

        for h in confluence_hits:
            entry = h["entry"]
            tl = h["tl"]
            aline = [[h["candles"][tl["i1"]]["t"], tl["p1"]], [h["candles"][-1]["t"], tl["trend_val"]]]
            link = chart_link(entry["ysym"], entry["label"], hline=h["level"]["price"], aline=aline)
            try:
                png = render_chart_png(
                    h["candles"], entry["ysym"],
                    hline=h["level"]["price"], aline=(tuple(aline[0]), tuple(aline[1])),
                )
            except Exception as e:
                print(f"[confluence] chart render failed for {entry['label']}: {e}")
                png = None
            items.append({
                "header": f"🎯 {entry['label']} ({entry['ysym'].replace('.T', '')}) — トレンドライン×水平線の交点",
                "text": build_confluence_reason(h),
                "chart_link": link,
                "png": png,
                "asset_type": "stock",
                "ysym": entry["ysym"],
                "hline": h["level"]["price"],
                "aline": aline,
            })

        for h in rsi_hits:
            link = chart_link(h["ysym"], h["label"])
            try:
                png = render_chart_png(h["candles"], h["ysym"])
            except Exception as e:
                print(f"[rsi] chart render failed for {h['label']}: {e}")
                png = None
            items.append({
                "header": f"📉 {h['label']} ({h['ysym'].replace('.T', '')}) — 週足RSI{h['rsi']:.0f}(売られすぎ)",
                "text": build_rsi_reason(h),
                "chart_link": link,
                "png": png,
                "asset_type": "stock",
                "ysym": h["ysym"],
                "hline": None,
                "aline": None,
            })

        for h in top:
            aline = None
            if h["line_type"] == "trend":
                aline = [[h["candles"][h["i1"]]["t"], h["p1"]], [h["candles"][-1]["t"], h["price"]]]
            link = chart_link(
                h["ysym"], h["label"],
                hline=h["price"] if h["line_type"] != "trend" else None,
                aline=aline,
            )
            try:
                if aline:
                    png = render_chart_png(h["candles"], h["ysym"], aline=(tuple(aline[0]), tuple(aline[1])))
                else:
                    png = render_chart_png(h["candles"], h["ysym"], hline=h["price"])
            except Exception as e:
                print(f"[scan] chart render failed for {h['label']}: {e}")
                png = None
            items.append({
                "header": f"■ {h['label']} ({h['ysym'].replace('.T', '')})",
                "text": build_reason(h),
                "chart_link": link,
                "png": png,
                "asset_type": "stock",
                "ysym": h["ysym"],
                "hline": h["price"] if h["line_type"] != "trend" else None,
                "aline": aline,
            })

        subject_bits = []
        if confluence_hits:
            subject_bits.append(f"🎯交点{len(confluence_hits)}件")
        if rsi_hits:
            subject_bits.append(f"📉RSI30割れ{len(rsi_hits)}件")
        if top:
            subject_bits.append(f"支持線接近{len(top)}件")
        lead = (
            confluence_hits[0]["entry"]["label"] if confluence_hits
            else rsi_hits[0]["label"] if rsi_hits
            else top[0]["label"]
        )
        subject = f"📊 今日の週足スキャン({'+'.join(subject_bits)}): {lead}など"
        if to_addr:
            try:
                send_digest_email(gmail_user, gmail_pass, to_addr, subject, intro_lines, items)
                print("sent digest email")
            except Exception as e:
                print(f"email send failed: {e}")
        else:
            print("notifyEmail not set, skipping email")

    # ヒット0件の日も履歴に残す(WEB側の「この日はヒットなしでした」表示に対応させるため、
    # 早期returnせず必ずpush_scan_historyまで到達させる)
    try:
        push_scan_history(alert_key, "market", jst_today_str(), items)
        print("saved scan history")
    except Exception as e:
        print(f"scan history save failed: {e}")


if __name__ == "__main__":
    main()
