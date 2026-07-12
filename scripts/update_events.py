#!/usr/bin/env python3
"""
seminar.html (https://www.nfuca-tokyo.jp/seminar.html) を巡回し、
index.html 内の EVENTS 配列に「未収録のお知らせ」を新規追加する。

設計方針:
- 既存の EVENTS に含まれる news_detail の URL はそのまま尊重し、上書きしない
  （手動で調整された時刻/会場/締切分割などの情報を壊さないため）。
- 新しく見つかった news_detail のみ、詳細ページ本文からベストエフォートで
  日時・会場・締切・カテゴリを抽出して追加する。
- 抽出の確信度が低い項目は "要確認" マーカーを付け、後続のPR本文で
  レビュアーに知らせる。
"""
import os
import re
import sys
import json
import unicodedata
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 is required (pip install beautifulsoup4)", file=sys.stderr)
    sys.exit(1)

BASE = "https://www.nfuca-tokyo.jp"
SEMINAR_URL = f"{BASE}/seminar.html"
INDEX_PATH = "index.html"
JST = timezone(timedelta(hours=9))
# ワークフロー実行環境によっては /tmp や RUNNER_TEMP がステップ間で共有されないため、
# リポジトリ内の相対パスに書き出す（コミット前にワークフロー側で削除する）
SUMMARY_PATH = "update_summary.json"

CATEGORY_KEYWORDS = [
    ("tokyo", ["東京ブロック", "ブロック運営委員会", "スタート会議", "サマースクール", "かけこみ相談会"]),
    ("zenkoku", ["全国", "連合会", "共済セミナー", "共済連", "環境セミナー"]),
    ("minami", ["南エリア"]),
    ("sobu", ["総武エリア"]),
    ("musashino", ["武蔵野エリア"]),
    ("kitakou", ["北甲エリア"]),
]

# 一覧ページのタイトルは「【東京B:開催案内】〜」のような角括弧プレフィックスを持つ。
# プレフィックスの略号から確実にカテゴリを判定できる。
CATEGORY_PREFIX_MAP = [
    (re.compile(r"東京\s*B"), "tokyo"),
    (re.compile(r"全国"), "zenkoku"),
    (re.compile(r"南\s*A"), "minami"),
    (re.compile(r"総武\s*A"), "sobu"),
    (re.compile(r"武蔵野\s*A"), "musashino"),
    (re.compile(r"北甲\s*A"), "kitakou"),
]

# 開催報告・アーカイブ等、今後の予定として掲載する意味がないお知らせは除外する
SKIP_TITLE_KEYWORDS = ["開催報告", "アーカイブ"]

DATE_FIELD_LABELS = ["実施日", "開催日", "日時", "開催期間", "開催日時"]
VENUE_FIELD_LABELS = ["会場", "場所", "開催場所"]
DEADLINE_FIELD_LABELS = ["申込締切", "締切", "締め切り", "申込期限"]


def fetch(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; nfuca-calendar-bot/1.0)"})
    with urlopen(req, timeout=30) as res:
        raw = res.read()
    for enc in ("utf-8", "shift_jis", "cp932", "euc-jp"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def normalize(s):
    return unicodedata.normalize("NFKC", s or "").strip()


def parse_jp_date(text, default_year):
    """'2026年7月5日' / '2026/7/5' / '2026.7.5' / '7月5日' 等をISO日付に変換。見つからなければ None。"""
    text = normalize(text)
    m = re.search(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if m:
        mo, d = map(int, m.groups())
        try:
            return f"{default_year:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            return None
    return None


def extract_field(body_text, labels):
    for label in labels:
        m = re.search(rf"{label}[:：]?\s*([^\n]+)", body_text)
        if m:
            val = normalize(m.group(1))
            if val:
                return val
    return None


def guess_category(title, body_text):
    m = re.search(r"【([^:：】]+)[:：]", title)
    if m:
        prefix = m.group(1)
        for pattern, cat in CATEGORY_PREFIX_MAP:
            if pattern.search(prefix):
                return cat, True

    haystack = title + "\n" + body_text
    for cat, keywords in CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in haystack:
                return cat, True
    return "tokyo", False  # デフォルト・低確信度


DATE_LINE_RE = re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日")


def clean_title(raw_text):
    """一覧のリンクテキストから日付行・NEWバッジ行を除いた本文タイトルを取り出す。"""
    lines = [normalize(ln) for ln in raw_text.split("\n")]
    lines = [ln for ln in lines if ln and not DATE_LINE_RE.match(ln) and ln != "NEW"]
    return lines[-1] if lines else ""


def list_seminar_links(html):
    soup = BeautifulSoup(html, "html.parser")
    seen = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"news_detail_(\d+)\.html", a["href"])
        if not m:
            continue
        news_id = m.group(1)
        raw_text = a.get_text("\n")
        title = clean_title(raw_text)
        if not title:
            parent = a.find_parent()
            title = clean_title(parent.get_text("\n")) if parent else ""
        if news_id in seen or not title:
            continue
        if any(kw in title for kw in SKIP_TITLE_KEYWORDS):
            continue
        seen[news_id] = {
            "id": news_id,
            "title": title,
            "url": f"{BASE}/news/news_detail_{news_id}.html",
        }
    return list(seen.values())


def strip_category_prefix(title):
    return re.sub(r"^【[^】]*】\s*", "", title).strip()


def build_event_entries(item, today):
    detail_html = fetch(item["url"])
    soup = BeautifulSoup(detail_html, "html.parser")
    body_text = normalize(soup.get_text("\n"))

    date_field = extract_field(body_text, DATE_FIELD_LABELS)
    venue = extract_field(body_text, VENUE_FIELD_LABELS)
    deadline_field = extract_field(body_text, DEADLINE_FIELD_LABELS)

    event_date = parse_jp_date(date_field, today.year) if date_field else None
    deadline_date = parse_jp_date(deadline_field, today.year) if deadline_field else None

    category, confident = guess_category(item["title"], body_text)
    display_title = strip_category_prefix(item["title"]) or item["title"]

    entries = []
    needs_review = []

    if deadline_date:
        entries.append({
            "date": deadline_date,
            "name": f"⚠️ 締切：{display_title}",
            "time": "締切",
            "venue": "",
            "category": category,
            "url": item["url"],
            "isDeadline": True,
        })

    if event_date:
        entries.append({
            "date": event_date,
            "name": display_title,
            "time": date_field or "",
            "venue": venue or "",
            "category": category,
            "url": item["url"],
        })
    else:
        needs_review.append(f"{item['title']} ({item['url']}): 開催日を自動抽出できませんでした")

    if not confident:
        needs_review.append(f"{item['title']} ({item['url']}): カテゴリを自動推定できず tokyo を仮設定しました")
    if event_date and not venue:
        needs_review.append(f"{item['title']} ({item['url']}): 会場を自動抽出できませんでした")

    return entries, needs_review


def js_escape(s):
    return s.replace("\\", "\\\\").replace("'", "\\'")


def format_entry(e):
    parts = [
        f"date:'{js_escape(e['date'])}'",
        f"name:'{js_escape(e['name'])}'",
        f"time:'{js_escape(e['time'])}'",
        f"venue:'{js_escape(e['venue'])}'",
        f"category:'{js_escape(e['category'])}'",
        f"url:'{js_escape(e['url'])}'",
    ]
    if e.get("isDeadline"):
        parts.append("isDeadline:true")
    return "  { " + ", ".join(parts) + " },"


def main():
    today = datetime.now(JST).date()

    with open(INDEX_PATH, encoding="utf-8") as f:
        html = f.read()

    m = re.search(r"// AUTO:EVENTS:START\nconst EVENTS = \[(.*?)\];\n// AUTO:EVENTS:END", html, re.S)
    if not m:
        print("ERROR: AUTO:EVENTS markers not found in index.html", file=sys.stderr)
        sys.exit(1)
    existing_block = m.group(1)
    existing_ids = set(re.findall(r"news_detail_(\d+)\.html", existing_block))
    existing_lines = [ln for ln in existing_block.strip("\n").split("\n") if ln.strip()]

    try:
        seminar_html = fetch(SEMINAR_URL)
    except (URLError, HTTPError) as e:
        print(f"ERROR: failed to fetch {SEMINAR_URL}: {e}", file=sys.stderr)
        sys.exit(1)

    items = list_seminar_links(seminar_html)
    print(f"INFO: found {len(items)} news_detail links on seminar.html")
    new_items = [it for it in items if it["id"] not in existing_ids]
    print(f"INFO: {len(new_items)} are not yet in EVENTS: {[it['id'] for it in new_items]}")

    all_new_entries = []
    all_needs_review = []
    for item in new_items:
        try:
            entries, needs_review = build_event_entries(item, today)
        except (URLError, HTTPError) as e:
            print(f"WARN: failed to fetch detail page {item['url']}: {e}", file=sys.stderr)
            continue
        all_new_entries.extend(entries)
        all_needs_review.extend(needs_review)

    today_iso = today.isoformat()
    before_count = len(all_new_entries)
    all_new_entries = [e for e in all_new_entries if e["date"] >= today_iso]
    if len(all_new_entries) < before_count:
        print(f"INFO: dropped {before_count - len(all_new_entries)} entries dated in the past")

    # 同じ日付・同じ名前の重複（同一イベントについて複数のお知らせが出ている場合）を排除
    seen_keys = set()
    deduped_entries = []
    for e in all_new_entries:
        key = (e["date"], e["name"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped_entries.append(e)
    if len(deduped_entries) < len(all_new_entries):
        print(f"INFO: dropped {len(all_new_entries) - len(deduped_entries)} duplicate entries")
    all_new_entries = deduped_entries

    if not all_new_entries:
        print("INFO: no new events to add")
        with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
            json.dump({"changed": False, "new_items": [], "needs_review": []}, f, ensure_ascii=False)
        return

    all_new_entries.sort(key=lambda e: e["date"])
    new_lines = [format_entry(e) for e in all_new_entries]
    combined_lines = existing_lines + new_lines
    # 日付順に安定ソート（既存の並び + 新規をまとめて date で整列）
    def line_date(ln):
        dm = re.search(r"date:'([^']+)'", ln)
        return dm.group(1) if dm else ""
    combined_lines.sort(key=line_date)

    new_block = "\n" + "\n".join(combined_lines) + "\n"
    new_events_section = f"// AUTO:EVENTS:START\nconst EVENTS = [{new_block}];\n// AUTO:EVENTS:END"
    html = html[:m.start()] + new_events_section + html[m.end():]

    today_jp = f"{today.year}年{today.month}月{today.day}日"
    html = re.sub(
        r"const LAST_UPDATED = '[^']*'; // AUTO:LAST_UPDATED",
        f"const LAST_UPDATED = '{today_jp}'; // AUTO:LAST_UPDATED",
        html,
    )

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"INFO: added {len(all_new_entries)} new event entries")
    with open("update_summary.json", "w", encoding="utf-8") as f:
        json.dump({
            "changed": True,
            "new_items": [{"title": it["title"], "url": it["url"]} for it in new_items],
            "needs_review": all_needs_review,
        }, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
