# -*- coding: utf-8 -*-
"""全省運行表：抓 HCT 報表平台 RPT79 拆封櫃明細（起站 0000＝全省，一次查完），
每段路順以自身起站4碼歸站，分類正班/加班/增開/追加/過路，算延誤與封櫃節奏，
上傳 Supabase Edge Function runsheet（action=ingest_day 整日覆蓋，冪等）。

用法：
  python fetch_runsheet.py                     # 昨天
  python fetch_runsheet.py --date 20261007     # 指定單日
  python fetch_runsheet.py --from 20260801 --to 20260831   # 回填區間
  加 --dry-run 只存 JSON 不上傳。
2026-10-08 起改為全省模式（原為彰化4106+秀水4150兩站查詢）。
"""
import argparse
import html as htmlmod
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, date
from pathlib import Path

BASE = ("http://nls.hct.com.tw:8083/old/AA005?MemberShip="
        "89219%2c%e9%99%b3%e4%bf%a1%e5%8b%9d%2c8023%2c%e9%81%8b%e6%8c%87"
        "%2c8008%2c%e9%81%8b%e5%8b%99%2c0908%2c%e5%85%ac%e5%8f%b8%2c0%2c43")
SSL_CTX = ssl._create_unverified_context()  # 公司網路 TLS 攔截，打 Supabase 需略過驗證

CONFIG = json.loads((Path(__file__).parent / "config.local.json").read_text(encoding="utf-8"))


def post(body_pairs, retries=3):
    body = urllib.parse.urlencode(body_pairs).encode("utf-8")
    req = urllib.request.Request(BASE, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            if i == retries - 1:
                raise
            print(f"  重試 {i+1}: {e}", file=sys.stderr)
            time.sleep(5)


def query_report(params, page=None):
    pairs = list(params.items()) + [
        ("submitQ", "查詢"), ("SAMPLE_FILE", "null"),
        ("RPT_COND", "P1,P2,P3,P4,P5,P6,P7,P8,P9,P10,"),
        ("RPT_ID", "79"), ("TITLE_CLASS", "運務"), ("MERGE_TITLE", ""),
        ("DYNAMIC_FIELD", "N"), ("Query", "Y"), ("DropdownNameOfEditMode", "")]
    if page:
        pairs += [("Page", str(page)),
                  ("notNeedDesMemberShip", "Y"),
                  ("MemberShip", "89219,陳信勝,8023,運指,8008,運務,0908,公司,0,43")]
    return post(pairs)


TAG_RE = re.compile(r"<[^>]+>")


def parse_rows(page_html):
    m = re.search(r"<div  id ='BlockCenterDiv'.*?</table>", page_html, re.S)
    rows = []
    if m:
        for tr in re.finditer(r"<tr[^>]*>(.*?)</tr>", m.group(0), re.S):
            cells = [htmlmod.unescape(TAG_RE.sub("", c.group(1))).strip()
                     for c in re.finditer(r"<td[^>]*>(.*?)</td>", tr.group(1), re.S)]
            if cells:
                rows.append(cells)
    pm = re.search(r"第&nbsp;(\d+)/(\d+)&nbsp;頁", page_html)
    total = int(pm.group(2)) if pm else 1
    return rows, total


def fetch_day(day: str):
    """全省單日：起站/迄站 0000，逐頁撈完。"""
    params = {"P1": day, "P2": "0000", "P3": "0000", "P4": "0", "P5": "0",
              "P6": "0000", "P7": "0000", "P8": "1N", "P9": "1N", "P10": "1"}
    rows, total = parse_rows(query_report(params))
    data = rows[1:] if rows else []
    for p in range(2, total + 1):
        rp, _ = parse_rows(query_report(params, page=p))
        data += rp[1:]
    return [r for r in data if r and len(r) >= 34 and r[0] != "作業類別"]


def hm_to_min(txt):
    m = re.match(r"^(\d{1,2}):(\d{2})$", (txt or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def diff_min(later, earlier):
    """兩個 HH:MM 的分差，跨夜折返到 [-720, 720)。"""
    a, b = hm_to_min(later), hm_to_min(earlier)
    if a is None or b is None:
        return None
    return (a - b + 720) % 1440 - 720


def to_int(txt):
    t = (txt or "").strip()
    return int(t) if t.isdigit() else None


def classify(work_type, name):
    if "增開" in name:
        return "增開"
    if work_type == "加班車":
        return "加班"
    if work_type == "追加":
        return "追加"
    if work_type == "":
        return "過路"
    return "正班"


def build_legs(raw_rows):
    legs = []
    for c in raw_rows:
        name = c[1].strip()
        code = name.split()[0] if name.split() else name
        canceled = c[2].strip() == "取消"
        m = re.match(r"(\d{4})", c[5] or "")
        legs.append({
            "station": m.group(1) if m else "0000",   # 路順起站4碼
            "category": classify(c[0].strip(), name),
            "canceled": canceled,
            "trip_code": code,
            "trip_name": name,
            "container": None if canceled else c[2].strip(),
            "rt_from": c[3], "rt_to": c[4],
            "leg_from": c[5], "leg_to": c[6],
            "seal_no": c[7] or None,
            "cargo_type": c[10] or None,
            "platform_load": c[11] or None,
            "seal_time": c[12] or None, "seal_staff": c[13] or None,
            "load_pct": to_int(c[14]),
            "sched_dep": c[16] or None, "act_dep": c[17] or None,
            "sched_arr": c[18] or None, "act_arr": c[19] or None,
            "dep_delay_min": diff_min(c[17], c[16]),
            "seal_to_dep_min": diff_min(c[17], c[12]),
            "unload_start": c[20] or None, "unload_end": c[23] or None,
            "unload_min": to_int(c[26]),
            "cage_send": to_int(c[27]), "cage_relay": to_int(c[28]),
            "cold_send": to_int(c[29]), "cold_relay": to_int(c[30]),
            "platform_unload": c[31] or None,
            "driver_from": c[32] or None, "driver_to": c[33] or None,
        })
    return legs


def upload(day_iso: str, legs):
    payload = {"action": "ingest_day", "ingest": CONFIG["ingest_token"],
               "day": day_iso, "legs": legs}
    req = urllib.request.Request(CONFIG["edge_url"],
                                 data=json.dumps(payload).encode("utf-8"),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300, context=SSL_CTX) as r:
        return r.read().decode("utf-8")[:200]


def run_day(qdate: str, dry_run: bool):
    day_iso = f"{qdate[:4]}-{qdate[4:6]}-{qdate[6:]}"
    t0 = time.time()
    raw = fetch_day(qdate)
    legs = build_legs(raw)
    cats = {}
    for l in legs:
        k = l["category"] + ("(取消)" if l["canceled"] else "")
        cats[k] = cats.get(k, 0) + 1
    nst = len({l["station"] for l in legs})
    print(f"{day_iso} 全省: {len(legs)} 段／{nst} 站 {cats}（{time.time()-t0:.0f}s）")
    if dry_run:
        out = Path(__file__).parent / f"dry_{qdate}_all.json"
        out.write_text(json.dumps(legs, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  （dry-run）已存 {out.name}")
    else:
        print("  上傳:", upload(day_iso, legs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--from", dest="d_from")
    ap.add_argument("--to", dest="d_to")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.d_from:
        d = datetime.strptime(args.d_from, "%Y%m%d").date()
        end = datetime.strptime(args.d_to or args.d_from, "%Y%m%d").date()
        while d <= end:
            run_day(d.strftime("%Y%m%d"), args.dry_run)
            d += timedelta(days=1)
    else:
        qdate = args.date or (date.today() - timedelta(days=1)).strftime("%Y%m%d")
        run_day(qdate, args.dry_run)


if __name__ == "__main__":
    main()
