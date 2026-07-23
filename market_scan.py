"""
日次マーケットスキャン(1日1回、GitHub Actionsで実行)

対象: 日経225 + chart_check.html で🔔/👁登録している株式銘柄。
週足で自動的に支持線・抵抗線を検出し(chart_check.html「自動で線を引く」と同じロジック)、
現在価格が支持線に近づいている(=反発すればチャンスになりうる)銘柄を、反応回数が多い順に
上位だけメールでまとめて知らせる。

「反応回数が多い線ほど効く」というスクショ由来の考え方をそのままスコアにしているだけで、
AIによる勝率判定ではない。あくまで学習メモの考え方に沿った機械的な絞り込み。

必要な環境変数: ALERT_KEY, GMAIL_USER, GMAIL_APP_PASSWORD
"""
import os
import time

import requests

from main import DB_URL, detect_levels, fetch_stock_candles, send_email
from nikkei225 import NIKKEI225

NEAR_PCT = 0.02       # 支持線の±2%以内を「接近中」とみなす(日次スキャンなので🔔より少し広め)
MIN_TOUCHES = 2        # detect_levels() の強い線フィルタと同じ基準
TOP_N = 8               # メールに載せる件数
REQUEST_DELAY_SEC = 0.5  # Yahoo側のレート制限を避けるための間隔


def build_universe(doc: dict) -> list:
    universe = {ysym: name for ysym, name in NIKKEI225}
    for a in (doc.get("alerts") or []):
        if a.get("assetType") == "stock" and a.get("ysym"):
            universe.setdefault(a["ysym"], a.get("label") or a["ysym"])
    for w in (doc.get("watchlist") or []):
        if w.get("assetType") == "stock" and w.get("ysym"):
            universe.setdefault(w["ysym"], w.get("label") or w["ysym"])
    return list(universe.items())


def find_near_support(ysym: str, label: str) -> list:
    candles = fetch_stock_candles(ysym)
    levels = detect_levels(candles)
    if not levels or len(candles) < 30:
        return []
    current = candles[-1]["c"]

    hits = []
    for lv in levels:
        if lv["price"] <= 0 or lv["price"] > current:
            continue  # 支持線(現在値より下)だけを対象にする
        if lv["touches"] < MIN_TOUCHES:
            continue
        dist_pct = (current - lv["price"]) / lv["price"]
        if dist_pct <= NEAR_PCT:
            hits.append({
                "ysym": ysym, "label": label, "current": current,
                "price": lv["price"], "touches": lv["touches"],
                "kind": lv["kind"], "dist_pct": dist_pct,
            })
    return hits


def build_reason(hit: dict) -> str:
    kind_label = "上場来高値(ATH)圏での攻防線" if hit["kind"] == "ath" else f"週足で{hit['touches']}回反応した支持線"
    return (
        f"{kind_label} {hit['price']:.4g} の {hit['dist_pct'] * 100:.1f}%上に位置(現在値 {hit['current']:.4g})。"
        "反応回数が多い線ほど効きやすいという考え方に基づく抽出。"
        "実体で割り込まずに反発すればチャンス、実体で下に抜けたら早めに見切るのが基本(投資助言ではありません)。"
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
    for i, (ysym, label) in enumerate(universe):
        try:
            hits = find_near_support(ysym, label)
        except Exception as e:
            print(f"[scan] retry after failure for {label} ({ysym}): {e}")
            time.sleep(3)
            try:
                hits = find_near_support(ysym, label)
            except Exception as e2:
                print(f"[scan] failed for {label} ({ysym}): {e2}")
                hits = []
        all_hits.extend(hits)
        if (i + 1) % 25 == 0:
            print(f"[scan] progress {i + 1}/{len(universe)}")
        time.sleep(REQUEST_DELAY_SEC)

    all_hits.sort(key=lambda h: (-h["touches"], h["dist_pct"]))
    top = all_hits[:TOP_N]
    print(f"found {len(all_hits)} candidates, sending top {len(top)}")

    if not top:
        print("no candidates today, skipping email")
        return
    if not to_addr:
        print("notifyEmail not set, skipping email")
        return

    lines = [
        "今日の週足スキャンで、支持線に接近している銘柄です(反応回数が多い順)。",
        "※ 学習メモの考え方に沿った機械的な抽出であり、投資助言ではありません。",
        "",
    ]
    for h in top:
        lines.append(f"■ {h['label']} ({h['ysym'].replace('.T', '')})")
        lines.append("  " + build_reason(h))
        lines.append("")
    lines.append("チャートツールで確認: https://wyujiro-toushi-chart.web.app")

    body = "\n".join(lines)
    subject = f"📊 今日の支持線接近スキャン: {top[0]['label']}など{len(top)}件"
    try:
        send_email(gmail_user, gmail_pass, to_addr, subject, body)
        print("sent digest email")
    except Exception as e:
        print(f"email send failed: {e}")


if __name__ == "__main__":
    main()
