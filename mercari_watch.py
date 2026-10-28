"""
Mercari Japan 新品上架監控 + LINE 通知

用法：
  python mercari_watch.py --once              # 檢查一次（GitHub Actions 排程用）
  python mercari_watch.py --once --dry-run    # 只印出結果，不發 LINE、不更新 seen.json
  python mercari_watch.py --test-line         # 只發純文字連線測試

監控條件寫在 watchlist.json；已通知過的商品 ID 存在 seen.json。
LINE 金鑰從環境變數讀取：LINE_CHANNEL_ACCESS_TOKEN、LINE_USER_ID（GitHub Secrets 提供）。
沒有設定金鑰時只會印在畫面上。
"""
import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from datetime import timedelta, timezone
from pathlib import Path

import httpx
from mercapi import Mercapi
from mercapi.requests.search import SearchRequestData

BASE = Path(__file__).parent
WATCHLIST = BASE / "watchlist.json"
SEEN = BASE / "seen.json"
SEEN_KEEP = 500          # 每組條件最多記住幾個 ID
DELAY_BETWEEN = 2        # 每組條件之間間隔秒數，避免打太快
HEALTH_KEY = "_health"   # seen.json 裡記錄連續失敗次數的欄位
ALERT_AFTER = 3          # 連續失敗幾次發警告
MAX_CARDS = 12           # LINE carousel 上限
TW = timezone(timedelta(hours=8))
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
ACCENT = "#E5004F"
AUCTION_COLOR = "#B45309"


# ---------- 搜尋 ----------

def item_url(it) -> str:
    # Mercari Shops 的商品網址格式不同
    if it.item_type == "ITEM_TYPE_BEYOND":
        return f"https://jp.mercari.com/shops/product/{it.id_}"
    return f"https://jp.mercari.com/item/{it.id_}"


def is_auction(it) -> bool:
    # Mercari 對每件商品都會回傳 auction 欄位（一般商品是空的），
    # mercapi 也會把空的轉成物件，所以要用「有沒有截止時間」判斷才準確。
    auction = getattr(it, "auction", None)
    return auction is not None and getattr(auction, "bid_deadline", None) is not None


def item_price(it):
    if is_auction(it):
        highest = getattr(it.auction, "highest_bid", None)
        if highest:
            return highest
    return it.real_price


def passes_filters(it, w: dict) -> bool:
    if not w.get("include_auctions", True) and is_auction(it):
        return False
    title = it.name or ""
    for pat in w.get("must_match", []):
        if not re.search(pat, title, re.IGNORECASE):
            return False
    for pat in w.get("must_not_match", []):
        if re.search(pat, title, re.IGNORECASE):
            return False
    price = item_price(it)
    if price is not None:
        if w.get("price_min") is not None and price < w["price_min"]:
            return False
        if w.get("price_max") is not None and price > w["price_max"]:
            return False
    return True


async def search_new(m: Mercapi, w: dict):
    # 同一商品的不同叫法分開搜尋；全部成功才保存這組的已看紀錄。
    keywords = list(dict.fromkeys(w.get("keywords") or [w["keyword"]]))
    found = {}
    for index, keyword in enumerate(keywords):
        if index:
            await asyncio.sleep(DELAY_BETWEEN)
        res = await m.search(
            keyword,
            exclude=w.get("exclude") or None,
            price_min=w.get("price_min"),
            price_max=w.get("price_max"),
            categories=w.get("category_ids", []),
            item_conditions=w.get("item_conditions", []),
            sort_by=SearchRequestData.SortBy.SORT_CREATED_TIME,
            sort_order=SearchRequestData.SortOrder.ORDER_DESC,
            status=[SearchRequestData.Status.STATUS_ON_SALE],
        )
        print(f"搜尋回傳 {len(res.items)} 筆")
        for it in res.items:
            if passes_filters(it, w):
                found[it.id_] = it
    return list(found.values())


def to_record(it, w: dict) -> dict:
    rec = {
        "watch": w.get("name", w["id"]),
        "keyword": w["keyword"],
        "name": it.name or "(無標題)",
        "price": it.real_price or 0,
        "url": item_url(it),
        "thumbnail": (it.thumbnails or [None])[0],
        "is_auction": is_auction(it),
        "review_note": w.get("review_note", ""),
        "bid_deadline": None,
        "total_bid": None,
    }
    if rec["is_auction"]:
        a = it.auction
        rec["bid_deadline"] = a.bid_deadline.astimezone(TW).strftime("%m/%d %H:%M")
        rec["total_bid"] = getattr(a, "total_bid", None)
        if getattr(a, "highest_bid", None):
            rec["price"] = a.highest_bid
    return rec


# ---------- LINE ----------

def _text(text, **kw) -> dict:
    return {"type": "text", "text": str(text)[:2000] or " ", **kw}


def bubble(x: dict) -> dict:
    body = [
        _text(x["watch"], size="xxs", color="#888888"),
        _text(x["name"], weight="bold", size="sm", wrap=True, maxLines=3),
    ]
    if x.get("review_note"):
        body.append(_text(x["review_note"], size="xxs", color="#888888", wrap=True))
    if x["is_auction"]:
        bids = f"・{x['total_bid']} 次出價" if x.get("total_bid") is not None else ""
        body += [
            _text(f"拍賣 目前 ¥{x['price']:,}{bids}", size="sm", weight="bold", color=AUCTION_COLOR, wrap=True),
            _text(f"截止 {x['bid_deadline']}（台灣時間）", size="xs", color=AUCTION_COLOR),
            _text("購買資格與成交價格依商品頁及所在地規定", size="xxs", color="#888888"),
        ]
    else:
        body.append(_text(f"¥{x['price']:,}", size="lg", weight="bold", color=ACCENT))
    b = {
        "type": "bubble",
        "size": "kilo",
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "contents": [{
            "type": "button", "style": "primary", "height": "sm", "color": ACCENT,
            "action": {"type": "uri", "label": "查看商品", "uri": x["url"]},
        }]},
    }
    if x.get("thumbnail", "") and str(x["thumbnail"]).startswith("https://"):
        b["hero"] = {"type": "image", "url": x["thumbnail"], "size": "full",
                     "aspectRatio": "1:1", "aspectMode": "cover",
                     "action": {"type": "uri", "uri": x["url"]}}
    return b


def build_message(items: list[dict], title: str = "Mercari 新品") -> dict:
    bubbles = [bubble(x) for x in items]
    alt = f"{title} {len(items)} 件：{items[0]['name']}"[:400]
    return {"type": "flex", "altText": alt, "contents": {"type": "carousel", "contents": bubbles}}


def line_push(messages: list[dict]) -> bool:
    """送出 LINE 訊息。成功回傳 True；沒有金鑰或失敗回傳 False。

    僅發給 LINE_USER_ID 指定的收件人。
    """
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    user = os.environ.get("LINE_USER_ID", "").strip()
    if not token or not user:
        print("（未設定 LINE 金鑰，只印在畫面上）")
        return False
    url, payload = LINE_PUSH_URL, {"to": user, "messages": messages}
    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}", "X-Line-Retry-Key": str(uuid.uuid4())},
            json=payload,
            timeout=20,
        )
    except httpx.HTTPError as e:
        print(f"LINE 推播失敗：{type(e).__name__}", file=sys.stderr)
        return False
    if r.status_code == 200:
        print("LINE 推播成功")
        return True
    print(f"LINE 推播失敗：HTTP {r.status_code}", file=sys.stderr)
    return False


def notify(new_items: list[dict]) -> bool:
    print(f"準備通知 {len(new_items)} 筆候選商品")
    # 一件商品可能符合多組搜尋，只顯示一次；卡片分頁，不把超量商品標記後丟掉。
    unique = list({x["url"]: x for x in new_items}.values())
    messages = [build_message(unique[i:i + MAX_CARDS])
                for i in range(0, len(unique), MAX_CARDS)]
    for i in range(0, len(messages), 5):
        if not line_push(messages[i:i + 5]):
            return False
    return True


def alert(text: str) -> None:
    print(text)
    line_push([{"type": "text", "text": text}])


# ---------- 主流程 ----------

def load_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def load_watchlist() -> list[dict]:
    values = load_json(WATCHLIST, [])
    if not isinstance(values, list) or not all(isinstance(w, dict) for w in values):
        raise ValueError("商品設定必須是物件陣列")
    active, ids = [], set()
    for value in values:
        if value.get("enabled", True) is False:
            continue
        w = dict(value)
        wid = w.get("id")
        keyword = w.get("keyword")
        if not isinstance(wid, str) or not wid.strip() or wid == HEALTH_KEY or wid in ids:
            raise ValueError("每組商品須有不同且非空白的 id")
        if not isinstance(keyword, str) or not keyword.strip():
            raise ValueError("啟用商品須填寫 keyword")
        keywords = w.get("keywords", [])
        if not isinstance(keywords, list) or any(not isinstance(k, str) or not k.strip() for k in keywords):
            raise ValueError("keywords 必須是非空白搜尋詞陣列，或留空陣列")
        for field in ("must_match", "must_not_match"):
            patterns = w.get(field, [])
            if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
                raise ValueError("篩選規則必須是字串陣列")
            for pattern in patterns:
                re.compile(pattern)
        for field in ("price_min", "price_max"):
            price = w.get(field)
            if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0):
                raise ValueError("價格須為非負數字或 null")
        low, high = w.get("price_min"), w.get("price_max")
        if low is not None and high is not None and low > high:
            raise ValueError("價格下限不可大於上限")
        ids.add(wid)
        active.append(w)
    return active


async def check_once(dry_run: bool = False) -> int:
    active = load_watchlist()
    if not active:
        print("尚未啟用商品設定；不搜尋、不通知。")
        return 0
    seen = load_json(SEEN, {})
    m = Mercapi()
    all_new, errors, searched = [], 0, 0
    new_seen = dict(seen)
    baselines = []  # 本輪首次建立基準的條件 id

    for i, w in enumerate(active):
        wid = w["id"]
        try:
            items = await search_new(m, w)
            searched += 1
        except Exception as e:  # 被擋、簽章失效、網路錯誤
            errors += 1
            print(f"搜尋失敗：{type(e).__name__}", file=sys.stderr)
            continue

        known = set(seen.get(wid, []))
        first_run = wid not in seen
        fresh = [it for it in items if it.id_ not in known]
        print(f"本次 {len(items)} 筆符合，新品 {len(fresh)} 筆"
              + ("（首次執行，只建立基準不通知）" if first_run else ""))

        if first_run:
            baselines.append(wid)
        else:
            all_new += [to_record(it, w) for it in fresh]
        new_seen[wid] = ([it.id_ for it in fresh] + seen.get(wid, []))[:SEEN_KEEP]

        if i < len(active) - 1:
            await asyncio.sleep(DELAY_BETWEEN)

    if dry_run:
        print(f"試跑完成：{len(all_new)} 筆新品候選，不通知、不保存。")
        return errors

    # LINE 送出成功（或沒有新品）才記成「已通知」；失敗就保留舊紀錄，下一輪重送
    if all_new and not notify(all_new):
        reverted = dict(seen)
        for wid in baselines:  # 首次建立的基準不需要重送，仍然保存
            reverted[wid] = new_seen[wid]
        new_seen = reverted
        errors += 1

    # 健康狀態：全部搜尋都失敗才算一次失敗
    health = seen.get(HEALTH_KEY, {"fails": 0, "alerted": False})
    if active and searched == 0:
        health["fails"] += 1
        if health["fails"] >= ALERT_AFTER and not health["alerted"]:
            alert(f"⚠️ Mercari 監控異常：已連續 {health['fails']} 次搜尋失敗，請查看 GitHub Actions 紀錄。")
            health["alerted"] = True
    else:
        if health.get("alerted"):
            alert("✅ Mercari 監控已恢復正常。")
        health = {"fails": 0, "alerted": False}
    new_seen[HEALTH_KEY] = health

    SEEN.write_text(json.dumps(new_seen, ensure_ascii=False, indent=1), encoding="utf-8")
    return errors


async def test_line() -> int:
    """只測試 LINE，不搜尋商品、不修改紀錄。"""
    ok = line_push([{"type": "text", "text": "【Mercari 通知測試】連線成功"}])
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只檢查一次")
    ap.add_argument("--dry-run", action="store_true", help="不發 LINE、不寫入 seen.json")
    ap.add_argument("--test-line", action="store_true", help="發一則 LINE 測試通知")
    a = ap.parse_args()

    if a.test_line:
        sys.exit(asyncio.run(test_line()))
    if a.once or a.dry_run:
        errors = asyncio.run(check_once(a.dry_run))
        sys.exit(1 if errors else 0)

    ap.print_help()


if __name__ == "__main__":
    main()

