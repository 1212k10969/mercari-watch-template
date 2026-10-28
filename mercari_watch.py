"""
Mercari Japan 新品上架監控 + LINE / Discord 通知

用法：
  python mercari_watch.py --once              # 檢查一次（GitHub Actions 排程用）
  python mercari_watch.py --once --dry-run    # 只印出結果，不發通知、不更新 seen.json
  python mercari_watch.py --test-line         # 只發純文字連線測試
  python mercari_watch.py --test-discord      # 只發 Discord 連線測試

監控條件從 .env / WATCHLIST_JSON 讀取；去重紀錄加密保存。
LINE 金鑰從環境變數讀取：LINE_CHANNEL_ACCESS_TOKEN、LINE_USER_ID（GitHub Secrets 提供）。
Discord 使用 DISCORD_WEBHOOK_URL；可只設定其中一個通道，或同時設定兩者。
沒有設定通知通道時不推播，也不將新品記成已通知。
"""
import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
import uuid
from datetime import timedelta, timezone
from pathlib import Path

import httpx
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from mercapi import Mercapi
from mercapi.requests.search import SearchRequestData
from market_sources import TaiwanSearch

BASE = Path(__file__).parent
VERSION = "0.1.0"
WATCHLIST = BASE / "watchlist.json"
SEEN = BASE / "seen.json"
SEEN_KEEP = 500          # 每組條件最多記住幾個 ID
DELAY_BETWEEN = 2        # 每組條件之間間隔秒數，避免打太快
HEALTH_KEY = "_health"   # seen.json 裡記錄連續失敗次數的欄位
ALERT_AFTER = 3          # 連續失敗幾次發警告
MAX_CARDS = 12           # LINE carousel 上限
DISCORD_MAX_RETRIES = 2
DISCORD_MAX_RETRY_AFTER = 10  # 超過此秒數留待下輪，避免排程長時間等待
TW = timezone(timedelta(hours=8))
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
ACCENT = "#E5004F"
AUCTION_COLOR = "#B45309"


# ---------- 搜尋 ----------

def item_url(it) -> str:
    if getattr(it, "url", None):
        return it.url
    # Mercari Shops 的商品網址格式不同
    if it.item_type == "ITEM_TYPE_BEYOND":
        return f"https://jp.mercari.com/shops/product/{it.id_}"
    return f"https://jp.mercari.com/item/{it.id_}"


def item_identity(it) -> str:
    return ("shops:" if it.item_type == "ITEM_TYPE_BEYOND" else "") + it.id_


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


async def search_new(m: Mercapi, w: dict, taiwan=None):
    # 同一商品的不同叫法分開搜尋；全部成功才保存這組的已看紀錄。
    keywords = list(dict.fromkeys(w.get("keywords") or [w["keyword"]]))
    found = {}
    for index, keyword in enumerate(keywords):
        if index:
            await asyncio.sleep(DELAY_BETWEEN)
        if w.get("source", "jp") == "tw":
            if taiwan is None:
                async with TaiwanSearch() as client:
                    items = await client.search(keyword)
            else:
                items = await taiwan.search(keyword)
        else:
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
            items = res.items
        print(f"搜尋回傳 {len(items)} 筆")
        for it in items:
            if passes_filters(it, w):
                found[item_identity(it)] = it
    return list(found.values())


def to_record(it, w: dict) -> dict:
    rec = {
        "item_id": item_identity(it),
        "watch": w.get("name", w["id"]),
        "keyword": w["keyword"],
        "name": it.name or "(無標題)",
        "price": it.real_price or 0,
        "currency": getattr(it, "currency", "JPY"),
        "url": item_url(it),
        "links": {"tw" if getattr(it, "currency", "JPY") == "TWD" else "jp": item_url(it)},
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


# ---------- 通知 ----------

def _text(text, **kw) -> dict:
    return {"type": "text", "text": str(text)[:2000] or " ", **kw}


def price_label(x: dict) -> str:
    return ("NT$" if x.get("currency") == "TWD" else "¥") + f"{x['price']:,}"


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
            _text(f"拍賣 目前 {price_label(x)}{bids}", size="sm", weight="bold", color=AUCTION_COLOR, wrap=True),
            _text(f"截止 {x['bid_deadline']}（台灣時間）", size="xs", color=AUCTION_COLOR),
            _text("購買資格與成交價格依商品頁及所在地規定", size="xxs", color="#888888"),
        ]
    else:
        body.append(_text(price_label(x), size="lg", weight="bold", color=ACCENT))
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


def discord_push(message: dict) -> bool:
    """使用 incoming webhook 發送；不在紀錄中暴露網址或回應本文。"""
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("（未設定 Discord Webhook）")
        return False
    payload = {**message, "allowed_mentions": {"parse": []}}
    for attempt in range(DISCORD_MAX_RETRIES + 1):
        try:
            url = httpx.URL(webhook).copy_set_param("wait", "true")
            response = httpx.post(url, json=payload, timeout=20)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            print(f"Discord 推播失敗：{type(e).__name__}", file=sys.stderr)
            return False
        if response.status_code in (200, 204):
            print("Discord 推播成功")
            return True
        if response.status_code == 429 and attempt < DISCORD_MAX_RETRIES:
            delay = response.headers.get("Retry-After")
            try:
                body = response.json()
                if isinstance(body, dict):
                    delay = body.get("retry_after", delay)
            except ValueError:
                pass
            try:
                delay = float(delay)
            except (TypeError, ValueError):
                delay = None
            if delay is not None and math.isfinite(delay) and 0 <= delay <= DISCORD_MAX_RETRY_AFTER:
                time.sleep(delay)
                continue
        print(f"Discord 推播失敗：HTTP {response.status_code}", file=sys.stderr)
        return False
    return False


def build_discord_messages(items: list[dict]) -> list[dict]:
    """商品卡片依 Discord 的 10 embeds / 6000 字元上限分批。"""
    messages, embeds, characters = [], [], 0
    for x in items:
        links = x.get("links") or {"item": x["url"]}
        labels = {"jp": "日本站", "tw": "台灣站", "item": "查看商品"}
        link_text = " · ".join(f"[{labels.get(source, '查看商品')}]({url})" for source, url in links.items())
        description = "\n".join(filter(None, [str(x["watch"]), str(x.get("review_note") or "")]))
        description = description[:max(0, 4096 - len(link_text) - 2)] + "\n\n" + link_text
        embed = {
            "title": str(x["name"])[:256] or "(無標題)",
            "url": x["url"],
            "description": description,
            "color": int((AUCTION_COLOR if x["is_auction"] else ACCENT)[1:], 16),
            "fields": [{"name": "拍賣目前價格" if x["is_auction"] else "價格",
                        "value": price_label(x), "inline": True}],
        }
        if not embed["description"]:
            del embed["description"]
        if x["is_auction"]:
            embed["fields"].append({"name": "截止（台灣時間）",
                                     "value": x.get("bid_deadline") or "未提供", "inline": True})
            if x.get("total_bid") is not None:
                embed["fields"].append({"name": "出價次數", "value": str(x["total_bid"]), "inline": True})
        if str(x.get("thumbnail") or "").startswith("https://"):
            embed["image"] = {"url": x["thumbnail"]}
        size = len(embed["title"]) + len(embed.get("description", ""))
        size += sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
        if embeds and (len(embeds) == 10 or characters + size > 6000):
            messages.append({"embeds": embeds})
            embeds, characters = [], 0
        embeds.append(embed)
        characters += size
    if embeds:
        messages.append({"embeds": embeds})
    return messages


def push_notifications(line_messages: list[dict], discord_messages: list[dict]) -> bool:
    """各通道獨立嘗試；所有已設定通道成功才推進共用的已通知紀錄。"""
    results = []
    if any(os.environ.get(key, "").strip() for key in ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_USER_ID")):
        ok = True
        for i in range(0, len(line_messages), 5):
            if not line_push(line_messages[i:i + 5]):
                ok = False
                break
        results.append(ok)
    if os.environ.get("DISCORD_WEBHOOK_URL", "").strip():
        ok = True
        for message in discord_messages:
            if not discord_push(message):
                ok = False
                break
        results.append(ok)
    if not results:
        print("（未設定通知通道，不推播）")
        return False
    return all(results)


def notify(new_items: list[dict]) -> bool:
    print(f"準備通知 {len(new_items)} 筆候選商品")
    # 一件商品可能符合多組搜尋，只顯示一次；分頁不丟掉超量商品。
    by_id = {}
    for record in new_items:
        identity = record.get("item_id", record["url"])
        by_id[identity] = merge_records(by_id[identity], record) if identity in by_id else record
    unique = list(by_id.values())
    if not unique:
        return True
    line_messages = [build_message(unique[i:i + MAX_CARDS])
                     for i in range(0, len(unique), MAX_CARDS)]
    return push_notifications(line_messages, build_discord_messages(unique))


def merge_records(first: dict, other: dict) -> dict:
    result = {**first, "links": {**first.get("links", {}), **other.get("links", {})}}
    if not result.get("thumbnail"):
        result["thumbnail"] = other.get("thumbnail")
    return result


def alert(text: str) -> None:
    print(text)
    push_notifications([{"type": "text", "text": text}], [{"content": text[:2000]}])


# ---------- 主流程 ----------

def load_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def load_state() -> dict:
    if not SEEN.exists():
        return {}
    data = SEEN.read_bytes()
    key = os.environ.get("MONITOR_STATE_KEY", "").strip()
    try:
        if key:
            data = Fernet(key.encode("ascii")).decrypt(data)
        elif SEEN.suffix == ".enc":
            raise ValueError("MONITOR_STATE_KEY is required")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("Invalid monitor state")
        return result
    except (InvalidToken, ValueError, UnicodeError):
        raise ValueError("監聽紀錄無法解密或格式錯誤；請確認 MONITOR_STATE_KEY") from None


def save_state(state: dict) -> None:
    data = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    key = os.environ.get("MONITOR_STATE_KEY", "").strip()
    if key:
        data = Fernet(key.encode("ascii")).encrypt(data)
    elif SEEN.suffix == ".enc":
        raise ValueError("保存監聽紀錄需要 MONITOR_STATE_KEY")
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    temporary = SEEN.with_suffix(SEEN.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(SEEN)


def load_watchlist() -> list[dict]:
    raw = os.environ.get("WATCHLIST_JSON", "").strip()
    try:
        values = json.loads(raw) if raw else load_json(WATCHLIST, [])
    except ValueError:
        raise ValueError("WATCHLIST_JSON 或商品設定必須是有效 JSON") from None
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
        sources = w.get("sources", ["jp"])
        if not isinstance(sources, list) or not sources or any(source not in ("jp", "tw") for source in sources):
            raise ValueError("sources 必須包含 jp 或 tw")
        if len(sources) != len(set(sources)):
            raise ValueError("sources 不可重複")
        source_keywords = w.get("source_keywords", {})
        if not isinstance(source_keywords, dict) or any(source not in sources for source in source_keywords):
            raise ValueError("source_keywords 必須對應啟用的來源")
        for terms in source_keywords.values():
            if not isinstance(terms, list) or not terms or any(not isinstance(term, str) or not term.strip() for term in terms):
                raise ValueError("來源搜尋詞須為非空白字串陣列")
        ids.add(wid)
        active.append(w)
    return active


async def check_once(dry_run: bool = False) -> int:
    active = load_watchlist()
    if not active:
        print("尚未啟用商品設定；不搜尋、不通知。")
        return 0
    if not dry_run and SEEN.suffix == ".enc":
        key = os.environ.get("MONITOR_STATE_KEY", "").strip()
        if not key:
            raise ValueError("MONITOR_STATE_KEY is required")
        Fernet(key.encode("ascii"))
    seen = load_state()
    m = Mercapi()
    candidates, errors, searched = {}, 0, 0
    new_seen = dict(seen)
    previous_known = set(seen.get("_known", []))
    for wid, values in seen.items():
        if not wid.startswith("_") and isinstance(values, list):
            previous_known.update(values)
    known = set(previous_known)
    baseline_known = set(previous_known)
    baselines = list(seen.get("_baselines", []))
    baseline_records = {}
    source_outcomes = {}

    async with TaiwanSearch() as taiwan:
        for w in active:
            wid = w["id"]
            for source in w.get("sources", ["jp"]):
                source_watch = {**w, "source": source}
                source_watch["keywords"] = w.get("source_keywords", {}).get(source, w.get("keywords", []))
                # Legacy price bounds are JPY. Taiwan bounds must be supplied explicitly.
                if source == "tw" and (w.get("price_min") is not None or w.get("price_max") is not None):
                    errors += 1
                    source_outcomes.setdefault(source, []).append(False)
                    print("tw 搜尋失敗：台灣來源請使用不限價條件", file=sys.stderr)
                    continue
                try:
                    items = await search_new(m, source_watch, taiwan)
                    searched += 1
                    source_outcomes.setdefault(source, []).append(True)
                except Exception as e:
                    errors += 1
                    source_outcomes.setdefault(source, []).append(False)
                    print(f"{source} 搜尋失敗：{type(e).__name__}", file=sys.stderr)
                    continue
                token = wid + ":" + source
                legacy_jp = source == "jp" and wid in seen and "_baselines" not in seen
                first_run = token not in baselines and not legacy_jp
                identities = [item_identity(it) for it in items]
                count = sum(identity not in known for identity in identities)
                print(f"{source} 本次 {len(items)} 筆符合，新品 {count} 筆"
                      + ("（首次執行，只建立基準不通知）" if first_run else ""))
                if token not in baselines:
                    baselines.append(token)
                if first_run:
                    baseline_ids = [identity for identity in identities if identity not in candidates]
                    baseline_known.update(baseline_ids)
                    baseline_records.setdefault(wid, []).extend(baseline_ids)
                for it in items:
                    identity = item_identity(it)
                    if identity in candidates:
                        candidates[identity] = merge_records(candidates[identity], to_record(it, w))
                    elif not first_run and identity not in known:
                        candidates[identity] = to_record(it, w)
                    known.add(identity)
                previous_ids = new_seen.get(wid, [])
                fresh_ids = [identity for identity in identities if identity not in previous_ids]
                new_seen[wid] = list(dict.fromkeys(fresh_ids + previous_ids))[:SEEN_KEEP]
                await asyncio.sleep(DELAY_BETWEEN)

    if dry_run:
        print(f"試跑完成：{len(candidates)} 筆新品候選，不通知、不保存。")
        return errors

    # 所有已設定通道送出成功（或沒有新品）才記成「已通知」；失敗保留舊紀錄，下一輪重送
    if candidates and not notify(list(candidates.values())):
        reverted = dict(seen)
        for wid, identities in baseline_records.items():
            reverted[wid] = list(dict.fromkeys(identities + seen.get(wid, [])))[:SEEN_KEEP]
        new_seen = reverted
        known = baseline_known
        errors += 1
    new_seen["_known"] = sorted(known)
    new_seen["_baselines"] = baselines

    # 健康狀態：全部搜尋都失敗才算一次失敗
    health = dict(seen.get(HEALTH_KEY, {"fails": 0, "alerted": False}))
    if active and searched == 0:
        health["fails"] += 1
    else:
        health = {"fails": 0, "alerted": False}
    providers = seen.get(HEALTH_KEY, {}).get("sources", {})
    health["sources"] = {}
    for source, outcomes in source_outcomes.items():
        previous = providers.get(source, {"fails": 0, "alerted": False})
        if all(outcomes):
            if previous.get("alerted"):
                alert(f"✅ Mercari {source} 來源已恢復正常。")
            health["sources"][source] = {"fails": 0, "alerted": False}
        else:
            current = {"fails": previous.get("fails", 0) + 1, "alerted": previous.get("alerted", False)}
            if current["fails"] >= ALERT_AFTER and not current["alerted"]:
                alert(f"⚠️ Mercari {source} 來源已連續 {current['fails']} 次搜尋失敗，請查看執行紀錄。")
                current["alerted"] = True
            health["sources"][source] = current
    new_seen[HEALTH_KEY] = health

    save_state(new_seen)
    return errors


async def test_line() -> int:
    """只測試 LINE，不搜尋商品、不修改紀錄。"""
    ok = line_push([{"type": "text", "text": "【測試】Mercari LINE 通知連線成功"}])
    return 0 if ok else 1


async def test_discord() -> int:
    """只測試 Discord，不搜尋商品、不修改紀錄。"""
    ok = discord_push({"content": "【測試】Mercari Discord 通知連線成功"})
    return 0 if ok else 1


def main():
    global SEEN
    load_dotenv(BASE / ".env", override=False, interpolate=False)
    SEEN = BASE / os.environ.get("MONITOR_STATE_PATH", ".state/seen.enc")
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", action="version", version=VERSION)
    ap.add_argument("--once", action="store_true", help="只檢查一次")
    ap.add_argument("--dry-run", action="store_true", help="不發通知、不寫入 seen.json")
    ap.add_argument("--test-line", action="store_true", help="發一則 LINE 測試通知")
    ap.add_argument("--test-discord", action="store_true", help="發一則 Discord 測試通知")
    a = ap.parse_args()

    if a.test_line or a.test_discord:
        results = []
        if a.test_line:
            results.append(asyncio.run(test_line()))
        if a.test_discord:
            results.append(asyncio.run(test_discord()))
        sys.exit(1 if any(results) else 0)
    if a.once or a.dry_run:
        try:
            errors = asyncio.run(check_once(a.dry_run))
        except Exception as e:
            print(f"監控未完成：{type(e).__name__}", file=sys.stderr)
            sys.exit(1)
        sys.exit(1 if errors else 0)

    ap.print_help()


if __name__ == "__main__":
    main()

