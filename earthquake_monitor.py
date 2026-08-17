#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
earthquake_monitor.py
tenki.jp の震度4以上の地震一覧を監視し、新規地震をAsanaプロジェクトメッセージに投稿する
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import json
import os
import re
import sys

# ── 設定 ──────────────────────────────────────────────
ASANA_TOKEN = os.environ["ASANA_TOKEN"]
PROJECT_GID = os.environ["PROJECT_GID"]
SECTION_GID = os.environ["SECTION_GID"]
ASSIGNEE_EMAIL = os.environ["ASSIGNEE_EMAIL"]
CUTOFF_DATE = "2026-04-01"
EARTHQUAKE_URL = "https://earthquake.tenki.jp/bousai/earthquake/entries/level-4/"
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_earthquakes.json")
BASE_URL = "https://earthquake.tenki.jp"
TIMEOUT = 30


def _build_session() -> requests.Session:
    """GETのみ自動リトライするセッション。

    POSTはリトライ対象外にしている。タスク作成POSTがタイムアウトした場合、
    Asana側では作成成功している可能性があり、再送すると重複タスクになるため。
    POSTの失敗は呼び出し側で捕捉し、次回の実行で拾い直す。
    """
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,  # 1秒 → 2秒 → 4秒
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


SESSION = _build_session()

# 長いキーが短いキーに部分一致するのを防ぐため、文字列長の降順で定義する
INTENSITY_MAP = {
    "level_6_minus": "震度6弱",
    "level_5_minus": "震度5弱",
    "level_6_plus":  "震度6強",
    "level_5_plus":  "震度5強",
    "level_7":       "震度7",
    "level_4":       "震度4",
    "level_3":       "震度3",
    "level_2":       "震度2",
    "level_1":       "震度1",
}


def is_transient(e: Exception) -> bool:
    """一時的な通信エラー（Asana/tenki.jp側の瞬断）かどうか"""
    if isinstance(e, (requests.Timeout, requests.ConnectionError, requests.exceptions.RetryError)):
        return True
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return e.response.status_code == 429 or e.response.status_code >= 500
    return False


def get_assignee_gid() -> str:
    res = SESSION.get(
        f"https://app.asana.com/api/1.0/users/{ASSIGNEE_EMAIL}",
        headers={"Authorization": f"Bearer {ASANA_TOKEN}", "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    res.raise_for_status()
    return res.json()["data"]["gid"]


def load_seen():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            try:
                return set(json.load(f))
            except json.JSONDecodeError:
                return set()
    return set()


def save_seen(seen):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def parse_intensity(img_src):
    # 長いキー（level_5_minus など）を先に評価するため INTENSITY_MAP はソート済み
    for key, label in INTENSITY_MAP.items():
        if key in img_src:
            return label
    return "不明"


def fetch_earthquakes():
    headers = {"User-Agent": "Mozilla/5.0 (compatible; EarthquakeMonitor/1.0)"}
    res = SESSION.get(EARTHQUAKE_URL, headers=headers, timeout=TIMEOUT)
    res.raise_for_status()
    res.encoding = "utf-8"
    soup = BeautifulSoup(res.text, "html.parser")

    earthquakes = []

    for a_tag in soup.find_all("a", href=re.compile(r"/bousai/earthquake/detail/")):
        href = a_tag.get("href", "")
        # URL末尾のファイル名をIDとして使用
        eq_id = href.strip("/").split("/")[-1].replace(".html", "")

        # 親の行またはセクションから情報を収集
        parent = (
            a_tag.find_parent("tr")
            or a_tag.find_parent("li")
            or a_tag.find_parent("section")
            or a_tag.find_parent("article")
        )
        if not parent:
            continue

        text = parent.get_text(separator=" ", strip=True)

        # 発生時刻
        time_text = a_tag.get_text(strip=True)

        # カットオフ日付より古い地震はスキップ
        date_match = re.search(r'(\d{4})年(\d{2})月(\d{2})日', time_text)
        if not date_match:
            continue
        eq_date = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
        if eq_date < CUTOFF_DATE:
            continue

        # マグニチュード
        m_match = re.search(r'M\s*([\d.]+)', text)
        magnitude = f"M{m_match.group(1)}" if m_match else "不明"

        # 最大震度（画像ファイル名から判定）
        img = parent.find("img", src=re.compile(r"level_"))
        intensity = parse_intensity(img["src"]) if img else "不明"

        # 震源地（時刻・M値・震度の文字列を除いた残り）
        location = text
        location = re.sub(r'\d{4}年\d{2}月\d{2}日\d{2}時\d{2}分頃?', '', location)
        location = re.sub(r'M\s*[\d.]+', '', location)
        location = re.sub(r'震度[\d弱強]+', '', location)
        location = re.sub(r'\s+', ' ', location).strip()

        earthquakes.append({
            "id":        eq_id,
            "time":      time_text,
            "date":      eq_date,
            "location":  location,
            "magnitude": magnitude,
            "intensity": intensity,
            "url":       BASE_URL + href,
        })

    return earthquakes


def post_to_asana(eq, assignee_gid):
    name = f"【地震情報】{eq['intensity']} {eq['location']}"
    notes = (
        f"発生時刻　: {eq['time']}\n"
        f"震源地　　: {eq['location']}\n"
        f"マグニチュード: {eq['magnitude']}\n"
        f"最大震度　: {eq['intensity']}\n"
        f"詳細　　　: {eq['url']}"
    )

    headers = {
        "Authorization": f"Bearer {ASANA_TOKEN}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

    # タスク作成
    res = SESSION.post(
        "https://app.asana.com/api/1.0/tasks",
        headers=headers,
        json={"data": {
            "name":     name,
            "notes":    notes,
            "projects": [PROJECT_GID],
            "assignee": assignee_gid,
            "due_on":   eq["date"],
        }},
        timeout=TIMEOUT,
    )
    res.raise_for_status()
    task_gid = res.json()["data"]["gid"]

    # セクションに追加
    SESSION.post(
        f"https://app.asana.com/api/1.0/sections/{SECTION_GID}/addTask",
        headers=headers,
        json={"data": {"task": task_gid}},
        timeout=TIMEOUT,
    ).raise_for_status()

    print(f"[OK] Asanaに投稿: {eq['time']} {eq['location']} {eq['intensity']}")


def main():
    seen = load_seen()

    # 担当者GIDの取得（Asanaの一時的な不調なら次回の実行で拾い直すため正常終了する）
    try:
        assignee_gid = get_assignee_gid()
    except Exception as e:
        if is_transient(e):
            print(f"[WARN] Asanaが一時的に応答しないため今回はスキップ: {e}", file=sys.stderr)
            return
        print(f"[ERROR] 担当者GIDの取得に失敗: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        earthquakes = fetch_earthquakes()
    except Exception as e:
        if is_transient(e):
            print(f"[WARN] ページ取得が一時的に失敗したため今回はスキップ: {e}", file=sys.stderr)
            return
        print(f"[ERROR] ページ取得失敗: {e}", file=sys.stderr)
        sys.exit(1)

    new_seen = set(seen)
    posted = 0

    for eq in earthquakes:
        if eq["id"] not in seen:
            try:
                post_to_asana(eq, assignee_gid)
                new_seen.add(eq["id"])
                posted += 1
            except Exception as e:
                # 投稿できなかった地震はseenに入れないので、次回の実行で再投稿される
                print(f"[ERROR] Asana投稿失敗 ({eq['id']}): {e}", file=sys.stderr)

    save_seen(new_seen)

    if posted == 0:
        print("[INFO] 新規地震なし")
    else:
        print(f"[INFO] {posted}件の新規地震を通知しました")


if __name__ == "__main__":
    main()
