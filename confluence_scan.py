"""
「トレンドライン x 水平線の交点」反発スキャン(1日1回、GitHub Actionsで実行)

対象: 主要指数(S&P500・NYダウ・NASDAQ・日経225・ハンセン・DAX・金) + 主要仮想通貨(BTC/ETH/SOL/XRP)。

market_scan.py は「水平線に近い」「トレンドラインに近い」をそれぞれ別々にチェックしているが、
このスキャンは分析メモ・じんのNotionコラムに出てくる「トレンドラインのサポートと水平線が
交わる付近まで価格が落ちてきている(カチカチだから反発しやすい)」という、より絞り込んだ
セットアップだけを探す。

トレンドラインは main.py の detect_trendlines()(期間全体で最もタッチ数の多い1本を選ぶ)ではなく、
このファイル独自に「直近の安値どうしを結ぶ、割り込まれていない上昇支持線」を
下側凸包(lower convex hull)で求める。detect_trendlines() は履歴全体で最も頑健な1本を選ぶため
古い年代の線になりがちで、"今まさに接近しているか" を見るこのスキャンの目的には合わない。

条件:
1. 週足の安値の下側凸包から求めた直近の上昇トレンドライン(サポート)の現在値換算と、
   水平線(複数回反応した価格帯)が現在値の3%以内まで接近している(=2本の線が交わっている)
2. 現在値がその交点圏(5%以内)にある
3. 直近6本(週)で価格が下げてきている(=上からの押し目であり、たまたま近いだけではない)

必要な環境変数: ALERT_KEY, GMAIL_USER, GMAIL_APP_PASSWORD
"""
import os

import requests

from main import (
    DB_URL, asset_symbol, chart_link, detect_swing_curve, fetch_candles_for, find_pivots,
    jst_today_str, push_scan_history, render_chart_png, send_digest_email,
)

LINES_GAP_PCT = 0.03    # トレンドラインと水平線が「交わっている」とみなす近さ
NOW_GAP_PCT = 0.05      # 現在値が交点圏内にあるとみなす近さ
DECLINE_LOOKBACK = 6    # 何本前と比べて「下げてきている」を判定するか
PIVOT_K = 2             # find_pivots の前後本数(main.pyのdetect_levels/detect_trendlinesと揃える)
LEVEL_TOL_PCT = 0.02    # 水平線としてまとめる価格の近さ(±2%)
LEVEL_MIN_TOUCHES = 2

# main.py の detect_levels() は「反応回数が多い順の上位4本ずつ」に絞るため、
# 直近にできたばかりでタッチ数が少ない水平線が候補から漏れてしまう。
# このスキャンは"直近のトレンドラインとの交点"を見たいので、タッチ数の上限フィルタなしで
# 価格の近さだけでクラスタリングする独自ロジックを使う。


def cluster_levels(points, tol_pct=LEVEL_TOL_PCT):
    """points: [(price, idx), ...] を価格の近さでまとめる。戻り値: [(level, [idx,...]), ...]"""
    if not points:
        return []
    pts = sorted(points)
    clusters = [[pts[0]]]
    for p in pts[1:]:
        mean_price = sum(q[0] for q in clusters[-1]) / len(clusters[-1])
        if abs(p[0] - mean_price) / mean_price <= tol_pct:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [(sum(q[0] for q in cl) / len(cl), [q[1] for q in cl]) for cl in clusters]

MAJOR_UNIVERSE = [
    {"assetType": "stock", "ysym": "^GSPC", "label": "S&P500(米国株)"},
    {"assetType": "stock", "ysym": "^DJI", "label": "NYダウ(米国株)"},
    {"assetType": "stock", "ysym": "^IXIC", "label": "NASDAQ総合(米国株)"},
    {"assetType": "stock", "ysym": "^N225", "label": "日経225"},
    {"assetType": "stock", "ysym": "^HSI", "label": "ハンセン指数(香港)"},
    {"assetType": "stock", "ysym": "^GDAXI", "label": "DAX(ドイツ)"},
    {"assetType": "stock", "ysym": "GC=F", "label": "金(ゴールド)"},
    {"assetType": "crypto", "sym": "BTCUSDT", "label": "ビットコイン"},
    {"assetType": "crypto", "sym": "ETHUSDT", "label": "イーサリアム"},
    {"assetType": "crypto", "sym": "SOLUSDT", "label": "ソラナ"},
    {"assetType": "crypto", "sym": "XRPUSDT", "label": "リップル"},
]


def lower_hull(points):
    """(x, y)点群の下側凸包 = どの安値にも割り込まれない、直近の上昇支持線の候補列。"""
    pts = sorted(points)
    hull = []
    for p in pts:
        while len(hull) >= 2:
            o, a = hull[-2], hull[-1]
            cross = (a[0] - o[0]) * (p[1] - o[1]) - (a[1] - o[1]) * (p[0] - o[0])
            if cross <= 0:
                hull.pop()
            else:
                break
        hull.append(p)
    return hull


def active_support_trendline(candles: list) -> dict | None:
    n = len(candles)
    pivots = find_pivots(candles, PIVOT_K)
    low_pts = [(p["i"], p["p"]) for p in pivots if not p["hi"]]
    hull = lower_hull(low_pts)
    if len(hull) < 2:
        return None
    (x1, y1), (x2, y2) = hull[-2], hull[-1]
    slope = (y2 - y1) / (x2 - x1)
    trend_val = y2 + slope * ((n - 1) - x2)
    return {"i1": x1, "p1": y1, "i2": x2, "p2": y2, "trend_val": trend_val, "touches": len(hull)}


def find_confluence(entry: dict) -> dict | None:
    candles = fetch_candles_for(entry)
    n = len(candles)
    if n < 30:
        return None
    current = candles[-1]["c"]

    tl = active_support_trendline(candles)
    if tl is None or tl["trend_val"] <= 0:
        return None

    recent_decline = current < candles[-DECLINE_LOOKBACK]["c"] if n > DECLINE_LOOKBACK else False
    if not recent_decline:
        return None

    pivots = find_pivots(candles, PIVOT_K)
    level_points = [(p["p"], p["i"]) for p in pivots]
    levels = [
        {"price": lv, "touches": len(idxs)}
        for lv, idxs in cluster_levels(level_points) if len(idxs) >= LEVEL_MIN_TOUCHES
    ]
    now_gap = abs(current - tl["trend_val"]) / current

    best = None
    for lv in levels:
        lines_gap = abs(lv["price"] - tl["trend_val"]) / current
        if lines_gap <= LINES_GAP_PCT and now_gap <= NOW_GAP_PCT:
            score = lines_gap + now_gap
            if best is None or score < best["score"]:
                best = {"level": lv, "lines_gap": lines_gap, "now_gap": now_gap, "score": score}
    if best is None:
        return None

    return {
        "entry": entry, "candles": candles, "current": current,
        "tl": tl, "level": best["level"], "lines_gap": best["lines_gap"], "now_gap": best["now_gap"],
    }


def build_reason(hit: dict) -> str:
    lv, tl = hit["level"], hit["tl"]
    return (
        f"トレンドライン(週足の安値を結んだ直近の上昇支持線、安値{tl['touches']}点を通過、"
        f"現在値換算 約{tl['trend_val']:.4g})と、水平線({lv['touches']}回反応、約{lv['price']:.4g})が"
        f"現在値の{hit['lines_gap']*100:.1f}%以内まで接近しています。"
        f"現在価格({hit['current']:.4g})はその交点の{hit['now_gap']*100:.1f}%圏内で、"
        "直近は上からこのゾーンに向けて下げてきています。"
        "教材の考え方でいう「トレンドラインと水平線の交点(カチカチ)」に近い状態で、反発しやすいと考えられます。"
        "ただし実体でこのゾーンを割り込んだ場合はシナリオ崩れとみなし、早めに見切るのが基本です。"
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

    hits = []
    for entry in MAJOR_UNIVERSE:
        try:
            hit = find_confluence(entry)
        except Exception as e:
            print(f"[confluence] failed for {entry['label']}: {e}")
            continue
        if hit:
            hits.append(hit)
            print(f"[hit] {entry['label']}")
        else:
            print(f"[no-hit] {entry['label']}")

    hits.sort(key=lambda h: h["lines_gap"] + h["now_gap"])

    if not hits:
        print("no confluence today, skipping email")
        return

    intro_lines = [
        "主要指数・仮想通貨の週足で「トレンドラインと水平線の交点」に価格が接近している銘柄です。",
        "※ 学習メモの考え方に沿った機械的な抽出であり、投資助言ではありません。",
    ]
    items = []
    for h in hits:
        entry = h["entry"]
        tl = h["tl"]
        aline = [[h["candles"][tl["i1"]]["t"], tl["p1"]], [h["candles"][-1]["t"], tl["trend_val"]]]
        recent = h["candles"][-60:]  # render_chart_png()のデフォルト表示本数(n=60)に合わせ、直近だけで曲線を作る
        curves = [c for c in (detect_swing_curve(recent, "sup"), detect_swing_curve(recent, "res")) if c]
        try:
            png = render_chart_png(
                h["candles"], asset_symbol(entry),
                hline=h["level"]["price"], aline=(tuple(aline[0]), tuple(aline[1])), curves=curves,
            )
        except Exception as e:
            print(f"[confluence] chart render failed for {entry['label']}: {e}")
            png = None
        items.append({
            "header": f"■ {entry['label']}",
            "text": build_reason(h),
            "chart_link": chart_link(entry),
            "png": png,
            "asset_type": entry.get("assetType"),
            "ysym": entry.get("ysym"),
            "sym": entry.get("sym"),
            "hline": h["level"]["price"],
            "aline": aline,
        })

    subject = f"📏 トレンドライン×水平線の交点: {hits[0]['entry']['label']}など{len(hits)}件"
    if to_addr:
        try:
            send_digest_email(gmail_user, gmail_pass, to_addr, subject, intro_lines, items)
            print("sent confluence digest email")
        except Exception as e:
            print(f"email send failed: {e}")
    else:
        print("notifyEmail not set, skipping email")

    try:
        push_scan_history(alert_key, "confluence", jst_today_str(), items)
        print("saved scan history")
    except Exception as e:
        print(f"scan history save failed: {e}")


if __name__ == "__main__":
    main()
