#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
earthquake_monitor.py
tenki.jp の震度4以上の地震一覧を監視し、新規地震をAsanaプロジェクトメッセージに投稿する
"""

import requests
from bs4 import BeautifulSoup
import json
import os
import re
import sys

# ── 設定 ──────────────────────────────────────────────
def _get_asana_token() -> str:
    return os.environ["ASANA_TOKEN"]


def _get_assignee_gid() -> str:
    res = requests.get(
        f"https://app.asana.com/api/1.0/users/{ASSIGNEE_EMAIL}",
        headers={"Authorization": f"Bearer {ASANA_TOKEN}", "Accept": "application/json"},
        timeout=15,
    )
    res.raise_for_status()
    return res.json()["data"]["gid"]


ASANA_TOKEN = _get_asana_token()
PROJECT_GID = os.environ["PROJECT_GID"]
SECTION_GID = os.environ["SECTION_GID"]
ASSIGNEE_EMAIL = os.environ["ASSIGNEE_EMAIL"]
CUTOFF_DATE = "2026-04-01"
EARTHQUAKE_URL = "https://earthquake.tenki.jp/bousai/earthquake/entries/level-4/"
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_earthquakes.json")
BASE_URL = "https://earthquake.tenki.jp"
ASSIGNEE_GID = _get_assignee_gid()

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
    res = requests.get(EARTHQUAKE_URL, headers=headers, timeout=15)
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


def post_to_asana(eq):
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
    res = requests.post(
        "https://app.asana.com/api/1.0/tasks",
        headers=headers,
        json={"data": {
            "name":     name,
            "notes":    notes,
            "projects": [PROJECT_GID],
            "assignee": ASSIGNEE_GID,
            "due_on":   eq["date"],
        }},
        timeout=15,
    )
    res.raise_for_status()
    task_gid = res.json()["data"]["gid"]

    # セクションに追加
    requests.post(
        f"https://app.asana.com/api/1.0/sections/{SECTION_GID}/addTask",
        headers=headers,
        json={"data": {"task": task_gid}},
        timeout=15,
    ).raise_for_status()

    print(f"[OK] Asanaに投稿: {eq['time']} {eq['location']} {eq['intensity']}")


def main():
    seen = load_seen()

    try:
        earthquakes = fetch_earthquakes()
    except Exception as e:
        print(f"[ERROR] ページ取得失敗: {e}", file=sys.stderr)
        sys.exit(1)

    new_seen = set(seen)
    posted = 0

    for eq in earthquakes:
        if eq["id"] not in seen:
            try:
                post_to_asana(eq)
                new_seen.add(eq["id"])
                posted += 1
            except Exception as e:
                print(f"[ERROR] Asana投稿失敗 ({eq['id']}): {e}", file=sys.stderr)

    save_seen(new_seen)

    if posted == 0:
        print("[INFO] 新規地震なし")
    else:
        print(f"[INFO] {posted}件の新規地震を通知しました")


if __name__ == "__main__":
    main()
