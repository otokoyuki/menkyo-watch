"""警視庁 学科試験予約の空き枠を見張り、新しく空いた枠を ntfy でスマホに通知する。

- 外部ライブラリ不要(Python 標準ライブラリのみ)
- 前回の結果を state.json に保存し、「新しく空いた枠」だけを通知する(同じ枠で何度も鳴らさない)
- 予約はしない。通知を見たら自分で予約する。

空き状況API(calgetres)はサーバー側で5分間キャッシュされ、予約サイトのカレンダーも同じデータを使う。
応答の currenttime がそのデータの作成時刻なので、条件(区分×試験場×月)ごとに
「作成時刻＋5分」の直後に見に行けば、作り直されたばかりのデータを読める(＝カレンダーに出た瞬間に気づける)。
各条件への問い合わせは5分に1回のまま。

環境変数:
  NTFY_TOPIC    通知先の ntfy トピック名(未設定なら通知せず画面に出すだけ)
  LOOP_MINUTES  指定すると、その分数だけ動き続けてキャッシュの作り直しに合わせて見張る(未指定なら1周して終わる)
  TEST_NOTIFY   "1" ならテスト通知を1本送って終わる
  SIMULATE      "1" なら実データを1周取ったうえで、2枠を「新しく空いた」ことにして通知する(状態は保存しない)
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
# 空きが出やすい 61 を先に見る
COURSES = {"61": "マイナ・両方", "11": "免許証のみ"}
MONTHS_AHEAD = 3  # 今月を含めて何ヶ月分見るか(11月なら 11・12・1月)
# -------------------------------------------------------------------------

CACHE_TTL = 300  # サーバー側キャッシュの寿命(秒)。2026-11-24 に実測
DUE_MARGIN = 2  # 作り直し時刻の何秒後に見に行くか
MIN_GAP = 1.5  # こちらの問い合わせ同士の最低間隔(秒)

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


def log(msg):
    print(msg, flush=True)


def target_months(now):
    y, m = now.year, now.month
    out = []
    for _ in range(MONTHS_AHEAD):
        out.append(f"{y}{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def targets(now):
    """見張る条件 (区分, 試験場, 月) の一覧。空きが出やすい順(61→11、近い月→遠い月)。"""
    return [(c, p, m) for c in COURSES for m in target_months(now) for p in PLACES]


def fetch_month(course, place, month):
    """(その月の枠の一覧, データの作成時刻[epoch秒]) を返す。"""
    q = urllib.parse.urlencode(
        {"date": month, "coursecode": course, "placecode": place, "user": "pub"}
    )
    req = urllib.request.Request(f"{API}?{q}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as res:
        data = json.loads(res.read().decode("utf-8"))
    if data.get("code") != "A0001":
        raise RuntimeError(f"想定外の応答 code={data.get('code')}")
    return data.get("body") or [], float(data.get("currenttime") or time.time())


def parse(course, place, rows, now, info):
    """{枠キー: 空き数} を返し、表示用の情報を info に入れる。予約できない過去の枠は除く。"""
    today = now.strftime("%Y%m%d")
    now_hhmm = now.strftime("%H%M")
    out = {}
    for row in rows:
        date, start = row["date"], row["starttime"]
        if date < today:
            continue
        if date == today and start <= now_hhmm:  # 当日でも受付時刻を過ぎた枠は除く
            continue
        key = f"{course}-{place}-{date}-{start}"
        out[key] = max(0, int(row["capacity"]) - int(row["reservation"]))
        ampm = "午前" if int(start) < 1000 else "午後"
        info[key] = (PLACES[place], date, ampm, COURSES[course], date == today)
    return out


def label(place_name, date, ampm, course_name, is_today):
    d = datetime.strptime(date, "%Y%m%d")
    mark = "【当日】" if is_today else ""
    return f"{mark}{d.month}/{d.day}({WEEKDAYS[d.weekday()]}) {ampm} {place_name}［{course_name}］"


def notify(title, message, priority=4, tags=("rotating_light",)):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    log(f"[通知] {title}\n{message}")
    if not topic:
        log("(NTFY_TOPIC 未設定のため送信はしません)")
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
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:  # 通知の失敗で見張りを止めない
        log(f"通知の送信に失敗: {e}")


def notify_found(found, slots, info, prefix=""):
    found = sorted(found, key=lambda k: (info[k][1], k))
    lines = [f"{label(*info[k])} 残り{slots[k]}" for k in found[:15]]
    if len(found) > 15:
        lines.append(f"ほか {len(found) - 15}枠")
    # 最高優先度: 端末側でサイレントモードを無視する設定にすれば深夜でも鳴る
    notify(f"{prefix}学科試験に空き {len(found)}枠", "\n".join(lines), priority=5)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"slots": {}, "error_notified": False}


def save_state(state, now):
    today = now.strftime("%Y%m%d")
    # 過ぎた日の枠は捨てる(キー = 区分-試験場-日付-開始時刻)
    state["slots"] = {k: v for k, v in state.get("slots", {}).items() if k.split("-")[2] >= today}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def set_error(state, failed, detail=""):
    """失敗が続いたら1回だけ知らせ、直ったら1回だけ知らせる。"""
    if failed and not state.get("error_notified"):
        notify("見張りが失敗しています", f"空き枠の取得に失敗しました。\n{detail}", 3, ("warning",))
        state["error_notified"] = True
    elif not failed and state.get("error_notified"):
        notify("見張りが復旧しました", "空き枠の取得が再びできるようになりました。", 2, ("white_check_mark",))
        state["error_notified"] = False


def run_once(state, simulate):
    """全条件を1周見る(テスト用・ループ指定なしのとき)。"""
    now = datetime.now(JST)
    known = dict(state.get("slots", {}))
    info, newly = {}, []
    first = True
    for course, place, month in targets(now):
        if not first:
            time.sleep(2)
        first = False
        rows, _ = fetch_month(course, place, month)
        batch = parse(course, place, rows, now, info)
        if simulate and not newly and batch:
            # 最初に取れた2枠を「前回は満員・今回1つ空いた」ことにする
            for k in sorted(batch, key=lambda k: (info[k][1], k))[:2]:
                known[k] = 0
                batch[k] = 1
        found = [k for k, free in batch.items() if free > 0 and known.get(k, 0) == 0]
        known.update(batch)
        if found:
            newly.extend(found)
            notify_found(found, batch, info, "【テスト】" if simulate else "")
    open_now = sum(1 for v in known.values() if v > 0)
    log(f"{now:%Y-%m-%d %H:%M} 1周確認 / 空きあり {open_now}枠 / 新しく空いた {len(newly)}枠")
    if not simulate:
        state["slots"] = known
        save_state(state, now)


def run_loop(state, minutes):
    """キャッシュの作り直しに合わせて、条件ごとに見に行き続ける。"""
    end = time.time() + minutes * 60
    known = state.setdefault("slots", {})
    info = {}
    due = {}  # 条件 → 次に見に行く時刻
    retries = {}  # 条件 → 作り直されていなかった回数
    last_req = 0.0
    errors = 0
    log(f"見張り開始: {minutes}分間、キャッシュの作り直しに合わせて確認します")

    while True:
        now = datetime.now(JST)
        current = targets(now)
        for t in current:
            due.setdefault(t, 0.0)
        for t in list(due):
            if t not in current:  # 月が替わって対象外になった条件
                del due[t]
        t = min(due, key=lambda k: (due[k], current.index(k)))
        start_at = max(due[t], last_req + MIN_GAP)
        if start_at >= end:
            break
        wait = start_at - time.time()
        if wait > 0:
            time.sleep(wait)

        course, place, month = t
        last_req = time.time()
        try:
            rows, made_at = fetch_month(course, place, month)
        except Exception as e:  # 通信エラーや仕様変更
            errors += 1
            log(f"取得失敗({errors}回連続) {course}-{place}-{month}: {e}")
            if errors >= 3:
                set_error(state, True, str(e))
            due[t] = time.time() + 30
            continue
        if errors:
            errors = 0
            set_error(state, False)

        fetched = time.time()
        age = fetched - made_at
        if age > CACHE_TTL + 10 and retries.get(t, 0) < 5:
            # 寿命を過ぎているのに作り直されていない(想定外)。少し待って取り直す
            retries[t] = retries.get(t, 0) + 1
            due[t] = fetched + 3
        else:
            retries[t] = 0
            next_refresh = made_at + CACHE_TTL + DUE_MARGIN
            # 想定外に古い/新しい時刻が返っても、問い合わせ間隔が極端にならないようにする
            due[t] = min(max(next_refresh, fetched + 5), fetched + CACHE_TTL + 60)

        now = datetime.now(JST)
        batch = parse(course, place, rows, now, info)
        found = [k for k, free in batch.items() if free > 0 and known.get(k, 0) == 0]
        known.update(batch)
        if found:
            notify_found(found, batch, info)
        open_now = sum(1 for v in batch.values() if v > 0)
        log(f"{now:%H:%M:%S} {COURSES[course]}・{PLACES[place]}・{month[4:]}月 データ{age:3.0f}秒前 空き{open_now}枠"
            + (f" ← 新しく空いた {len(found)}枠" if found else ""))
        save_state(state, now)

    log("見張り終了(次の実行に引き継ぎます)")


def main():
    if os.environ.get("TEST_NOTIFY") == "1":
        notify("テスト通知", "免許の空き枠見張りから届いています。タップで予約ページが開きます。", 3, ("white_check_mark",))
        return 0

    state = load_state()
    simulate = os.environ.get("SIMULATE") == "1"
    loop_minutes = float(os.environ.get("LOOP_MINUTES") or 0)

    if loop_minutes > 0 and not simulate:
        run_loop(state, loop_minutes)
        return 0

    try:
        run_once(state, simulate)
    except Exception as e:
        log(f"取得失敗: {e}")
        if not simulate:
            set_error(state, True, str(e))
            save_state(state, datetime.now(JST))
        return 1
    if not simulate:
        set_error(state, False)
        save_state(state, datetime.now(JST))
    return 0


if __name__ == "__main__":
    sys.exit(main())
