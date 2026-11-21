"""警視庁 学科試験予約の空き枠を見張り、新しく空いた枠を ntfy でスマホに通知する。

- 外部ライブラリ不要(Python 標準ライブラリのみ)
- 前回の結果を state.json に保存し、「新しく空いた枠」だけを通知する(同じ枠で何度も鳴らさない)
- 予約はしない。通知を見たら自分で予約する。

環境変数:
  NTFY_TOPIC   通知先の ntfy トピック名(未設定なら通知せず画面に出すだけ)
  TEST_NOTIFY  "1" ならテスト通知を1本送って終わる
  SIMULATE     "1" なら実データを取ったうえで、2枠を「新しく空いた」ことにして通知する(状態は保存しない)
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- 見張る条件 ----------------------------------------------------------
PLACES = {"270": "府中", "280": "鮫洲", "250": "江東"}
# 教習所卒業等の区分。予約サイトで「マイナ免許証のみ」「両方」を選ぶと 61 になる
COURSES = {"11": "免許証のみ", "61": "マイナ・両方"}
MONTHS_AHEAD = 3  # 今月を含めて何ヶ月分見るか(11月なら 11・12・1月)
# -------------------------------------------------------------------------

API = "https://license-test-tokyo-prd-police-pref-api.tokyo-madoguchi-yoyaku.com/calgetres"
RESERVE_PAGE = "https://license-test.tokyo-madoguchi-yoyaku.com/police-pref-tokyo/index.html?lang=ja"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json",
    "Origin": "https://license-test.tokyo-madoguchi-yoyaku.com",
    "Referer": "https://license-test.tokyo-madoguchi-yoyaku.com/police-pref-tokyo/01/html/main.html?lang=ja",
}
STATE_FILE = Path(__file__).with_name("state.json")
JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"


def target_months(now):
    y, m = now.year, now.month
    out = []
    for _ in range(MONTHS_AHEAD):
        out.append(f"{y}{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def fetch_month(course, place, month):
    q = urllib.parse.urlencode(
        {"date": month, "coursecode": course, "placecode": place, "user": "pub"}
    )
    req = urllib.request.Request(f"{API}?{q}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as res:
        data = json.loads(res.read().decode("utf-8"))
    if data.get("code") != "A0001":
        raise RuntimeError(f"想定外の応答 code={data.get('code')}")
    return data.get("body") or []


def collect(now):
    """{枠キー: 空き数} と、枠キー→表示用の情報 を返す。"""
    today = now.strftime("%Y%m%d")
    now_hhmm = now.strftime("%H%M")
    slots, info = {}, {}
    first = True
    for course, course_name in COURSES.items():
        for month in target_months(now):
            for place, place_name in PLACES.items():
                if not first:
                    time.sleep(2)  # サーバーに負担をかけないよう間を空ける
                first = False
                for row in fetch_month(course, place, month):
                    date, start = row["date"], row["starttime"]
                    if date < today:
                        continue
                    if date == today and start <= now_hhmm:  # 当日でも受付時刻を過ぎた枠は除く
                        continue
                    free = max(0, int(row["capacity"]) - int(row["reservation"]))
                    key = f"{course}-{place}-{date}-{start}"
                    slots[key] = free
                    ampm = "午前" if int(start) < 1000 else "午後"
                    info[key] = (place_name, date, ampm, course_name, date == today)
    return slots, info


def label(place_name, date, ampm, course_name, is_today):
    d = datetime.strptime(date, "%Y%m%d")
    mark = "【当日】" if is_today else ""
    return f"{mark}{d.month}/{d.day}({WEEKDAYS[d.weekday()]}) {ampm} {place_name}［{course_name}］"


def notify(title, message, priority=4, tags=("rotating_light",)):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    print(f"[通知] {title}\n{message}")
    if not topic:
        print("(NTFY_TOPIC 未設定のため送信はしません)")
        return
    body = json.dumps(
        {
            "topic": topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": list(tags),
            "click": RESERVE_PAGE,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://ntfy.sh/", data=body, headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=30).read()


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"slots": {}, "error_notified": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    if os.environ.get("TEST_NOTIFY") == "1":
        notify("テスト通知", "免許の空き枠見張りから届いています。タップで予約ページが開きます。", 3, ("white_check_mark",))
        return 0

    now = datetime.now(JST)
    state = load_state()

    try:
        slots, info = collect(now)
    except Exception as e:  # 通信エラーや仕様変更
        print(f"取得失敗: {e}", file=sys.stderr)
        if not state.get("error_notified"):
            notify("見張りが失敗しています", f"空き枠の取得に失敗しました。\n{e}", 3, ("warning",))
            state["error_notified"] = True
            save_state(state)
        return 1

    if state.get("error_notified"):
        notify("見張りが復旧しました", "空き枠の取得が再びできるようになりました。", 2, ("white_check_mark",))
        state["error_notified"] = False

    prev = state.get("slots", {})
    simulate = os.environ.get("SIMULATE") == "1"
    if simulate:
        # 本物の取得結果のうち先頭2枠を「前回は満員・今回1つ空いた」ことにする
        prev = dict(prev)
        slots = dict(slots)
        for k in sorted(slots, key=lambda k: (info[k][1], k))[:2]:
            prev[k] = 0
            slots[k] = 1

    newly = [k for k, free in slots.items() if free > 0 and prev.get(k, 0) == 0]
    open_now = [k for k, free in slots.items() if free > 0]

    print(f"{now:%Y-%m-%d %H:%M} 確認 {len(slots)}枠 / 空きあり {len(open_now)}枠 / 新しく空いた {len(newly)}枠")

    if newly:
        newly.sort(key=lambda k: (info[k][1], k))
        lines = [f"{label(*info[k])} 残り{slots[k]}" for k in newly[:15]]
        if len(newly) > 15:
            lines.append(f"ほか {len(newly) - 15}枠")
        prefix = "【テスト】" if simulate else ""
        notify(f"{prefix}学科試験に空き {len(newly)}枠", "\n".join(lines))

    if simulate:
        return 0  # 偽の空きを次回に持ち越さない
    state["slots"] = slots
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
