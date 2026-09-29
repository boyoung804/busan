#!/usr/bin/env python3
"""부산일보·국제신문 당일 기사 → 선별·분류·요약(규칙 기반, API 키 불필요) → docs/report.json

GitHub Actions에서 5분마다 실행된다.
- 선별: keywords.yml 의 키워드 가중치 + 지면(1면 등) 가점
- 요약: 기사 본문 앞부분 문장 발췌 (생성 요약이 아님)
- 묶기: 제목 글자 유사도로 두 신문의 같은 사안을 하나로 표기
- 사설·칼럼: 원문 그대로 게재
"""
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "report.json"
CACHE = ROOT / "data" / "cache.json"
KST = dt.timezone(dt.timedelta(hours=9))
UA = {"User-Agent": "press-brief-bot/1.0 (internal use)"}

TOP_N = 3                                       # '1. 주요 이슈(부산)'에 올릴 건수
QUOTA = {"시청·시의회": 5, "정치": 2, "경제": 3, "사회 일반": 2}   # 2페이지 섹션별 상한
MAX_NEW_PER_RUN = 40                            # 한 번에 새로 가져올 기사 수 상한


def now():
    return dt.datetime.now(KST)


def sha(s):
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def get_soup(url):
    r = requests.get(url, headers=UA, timeout=15)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return BeautifulSoup(r.text, "html.parser")


# ---------------------------------------------------------------- 1. 수집
def collect(cache):
    cfg = yaml.safe_load((ROOT / "sources.yml").read_text(encoding="utf-8"))
    today = now().strftime("%Y%m%d")
    fetched = 0
    for src in cfg["sources"]:
        a = src["article"]
        for lst in src["lists"]:
            url = lst["url"].replace("{date}", today)
            if url.startswith("TODO"):
                print(f"[skip] {src['name']} {lst['kind']}: sources.yml 미설정")
                continue
            try:
                soup = get_soup(url)
            except Exception as e:  # 한 곳이 실패해도 나머지는 진행
                print(f"[warn] 목록 실패 {url}: {e}")
                continue
            links = [urljoin(url, x["href"]) for x in soup.select(lst["link_selector"]) if x.get("href")]
            for link in dict.fromkeys(links):
                aid = sha(link)
                if aid in cache["articles"]:
                    continue
                if fetched >= MAX_NEW_PER_RUN:
                    return
                try:
                    s = get_soup(link)
                    title = s.select_one(a["title"]).get_text(" ", strip=True)
                    paras = [p.get_text(" ", strip=True) for p in s.select(a["body"])]
                    body = "\n".join(p for p in paras if p) or s.select_one(a["body"]).get_text("\n", strip=True)
                    page = ""
                    if a.get("page") and not a["page"].startswith("TODO"):
                        el = s.select_one(a["page"])
                        m = re.search(r"\d+", el.get_text()) if el else None
                        page = f"{m.group(0)}면" if m else ""
                except Exception as e:
                    print(f"[warn] 기사 실패 {link}: {e}")
                    continue
                cache["articles"][aid] = {
                    "media": src["media"], "kind": lst["kind"], "title": title,
                    "body": body, "page": page, "url": link,
                }
                fetched += 1
    print(f"[collect] 새 기사 {fetched}건")


# ---------------------------------------------------------------- 2. 규칙 기반 채점·분류·요약
def lead(body, limit):
    """본문 앞 문장들을 limit 글자 안에서 발췌."""
    text = " ".join(body.split())
    sents = re.split(r"(?<=[다요음임함됨]\.)\s+", text)
    out = ""
    for s in sents[:6]:
        if out and len(out) + len(s) + 1 > limit:
            break
        out = f"{out} {s}".strip()
        if len(out) >= limit * 0.6:
            break
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def score_article(a, cfg):
    title, head = a["title"], " ".join(a["body"].split())[:400]
    hits, score = [], 0
    for kw, w in cfg["weights"].items():
        in_t, in_b = kw in title, kw in head
        if in_t or in_b:
            score += w * (2 if in_t else 1)
            hits.append((w * (2 if in_t else 1), kw))
    if any(w in title for w in cfg["exclude"]["words"]):
        score -= cfg["exclude"]["penalty"]
    score += cfg.get("page_bonus", {}).get(a["page"], 0)
    hits.sort(reverse=True)

    text = title + " " + head
    best, best_n = "사회 일반", 0
    for sec, words in cfg["sections"].items():        # dict 순서 = 동점 시 우선순위
        n = sum(1 for w in words if w in text)
        if n > best_n:
            best, best_n = sec, n
    dept = next((d for kw, d in cfg["depts"].items() if kw in text), None)
    return {
        "score": min(score, 10),
        "section": best,
        "tag": hits[0][1] if hits else title[:10],
        "dept": dept,
        "summary": lead(a["body"], cfg["summary_max_chars"]),
    }


# ---------------------------------------------------------------- 3. 같은 사안 묶기 (제목 유사도)
def grams(s):
    s = re.sub(r"[^가-힣A-Za-z0-9]", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def nums(s):
    """'1조3783억', '100일' 처럼 구체적인 숫자 토큰 (같은 사안 판별에 강한 단서)."""
    return {n for n in re.findall(r"\d[\d,\.]{2,}", s)}


def sim(a, b):
    """a, b: (제목, 요약). 제목 유사도·제목+요약 유사도·공통 숫자를 종합."""
    t = _jac(grams(a[0]), grams(b[0]))
    x = _jac(grams(a[0] + a[1]), grams(b[0] + b[1]))
    bonus = 0.3 if nums(a[0] + a[1]) & nums(b[0] + b[1]) else 0.0
    return max(t, x) + bonus


def _jac(ga, gb):
    return len(ga & gb) / len(ga | gb) if ga and gb else 0.0


def cluster(ids, arts, sc, thr):
    groups = []
    key = lambda k: (arts[k]["title"], sc[k]["summary"])
    for i in sorted(ids, key=lambda k: sc[k]["score"], reverse=True):
        for g in groups:
            if any(sim(key(i), key(j)) >= thr for j in g):
                g.append(i)
                break
        else:
            groups.append([i])
    return groups


# ---------------------------------------------------------------- 4. report.json 조립
def assemble(cache, cfg):
    arts = cache["articles"]
    sc = {i: score_article(a, cfg) for i, a in arts.items() if a["kind"] == "news"}
    cands = [i for i, s in sc.items() if s["score"] >= cfg["threshold"]]
    groups = cluster(cands, arts, sc, cfg["cluster_similarity"])

    def gscore(g):
        s = max(sc[i]["score"] for i in g)
        s += len({arts[i]["media"] for i in g}) - 1        # 두 신문 공통 보도 가점
        return s

    groups.sort(key=gscore, reverse=True)
    dept = lambda g: next((sc[i]["dept"] for i in g if sc[i]["dept"]), None)

    busan = [{
        "tag": sc[g[0]]["tag"], "dept": dept(g), "title": sc[g[0]]["summary"],
        "lines": [{"m": arts[i]["media"], "t": arts[i]["title"], "p": arts[i]["page"], "u": arts[i]["url"]} for i in g],
    } for g in groups[:TOP_N]]

    by_sec, used = {k: [] for k in QUOTA}, {k: 0 for k in QUOTA}
    for g in groups[TOP_N:]:
        sec = sc[g[0]]["section"]
        if used[sec] < QUOTA[sec]:
            used[sec] += 1
            medias = "/".join(dict.fromkeys(arts[i]["media"] for i in g))
            by_sec[sec].append({
                "tag": sc[g[0]]["tag"], "dept": dept(g), "title": arts[g[0]]["title"],
                "u": arts[g[0]]["url"],
                "lines": [{"m": medias, "t": sc[g[0]]["summary"], "body": 1}],
            })
    local = [{"cat": k, "items": v} for k, v in by_sec.items() if v]

    opinion = {}
    for a in arts.values():
        if a["kind"] == "opinion":
            opinion.setdefault(a["media"] + " 오피니언", []).append({
                "tag": "사설", "title": a["title"], "u": a["url"],
                "lines": [{"m": a["media"], "t": " ".join(a["body"].split()), "body": 1, "op": 1}],  # 원문 그대로
            })

    return {
        "date": now().strftime("%Y. %-m. %-d.(") + "월화수목금토일"[now().weekday()] + ")",
        "issue": {"national": [], "busan": busan},
        "local": local,
        "central": [],
        "opinion": [{"cat": k, "items": v} for k, v in opinion.items()],
    }


def main():
    cfg = yaml.safe_load((ROOT / "keywords.yml").read_text(encoding="utf-8"))
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    today = now().strftime("%Y-%m-%d")
    if cache.get("day") != today:  # 날짜가 바뀌면 캐시 초기화
        cache = {"day": today, "articles": {}}

    collect(cache)
    report = assemble(cache, cfg)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    # 내용이 그대로면 파일을 건드리지 않는다 → 불필요한 커밋·Pages 재배포 방지
    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    old.pop("updated", None)
    if old != report:
        report["updated"] = now().strftime("%Y-%m-%d %H:%M")
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print("[done] report.json 갱신")
    else:
        print("[done] 변경 없음")


if __name__ == "__main__":
    sys.exit(main())
