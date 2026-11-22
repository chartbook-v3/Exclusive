# -*- coding: utf-8 -*-
"""
Exclusive - 대한민국 주요 26개 언론사 기업·자본시장 [단독] 속보 24시간 모니터링 봇
- 출력 대상: 텔레그램 채널 (@chartbooknews)
- 출력 양식: [단독] 언론사명 (하이퍼링크) + 기사 제목 (웹 미리보기 비활성화)
- 뉴스 수집용 별도 API 키 불필요 (공식 RSS 및 구글 뉴스 RSS 파싱)
- 텔레그램 발송용 TELEGRAM_BOT_TOKEN은 GitHub Secrets 환경변수로 안전하게 주입
"""

import os
import re
import sys
import html
import json
import time
import argparse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "sent_history.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")


def load_local_env():
    """로컬 테스트 시 .env 파일이 있으면 환경변수로 로드합니다 (GitHub 업로드 제외)."""
    if not os.path.exists(ENV_PATH):
        return
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'").strip('"')
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


load_local_env()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@chartbooknews").strip()
DEFAULT_MAX_AGE_MINUTES = 90
MAX_SEND_PER_RUN = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# 검증 완료된 26개 언론사 RSS (직접 공식 RSS 17개 + 구글 뉴스 전용 RSS 9개)
RSS_FEEDS = [
    # Tier 1: 종합·경제지 및 통신사
    ("연합뉴스", "https://www.yna.co.kr/rss/economy.xml", False),
    ("조선일보", "https://www.chosun.com/arc/outboundfeeds/rss/?outputType=xml", False),
    ("조선비즈", "https://biz.chosun.com/arc/outboundfeeds/rss/?outputType=xml", False),
    ("중앙일보", "https://news.google.com/rss/search?q=intitle:%EB%8B%A8%EB%8F%85+site:joongang.co.kr&hl=ko&gl=KR&ceid=KR:ko", True),
    ("동아일보", "https://rss.donga.com/economy.xml", False),
    ("한국경제", "https://www.hankyung.com/feed/all-news", False),
    ("매일경제", "https://www.mk.co.kr/rss/30000001/", False),
    # Tier 2: 유력 경제·종합지 및 양대 IT 일간지
    ("전자신문", "https://rss.etnews.com/Section902.xml", False),
    ("디지털타임스", "https://news.google.com/rss/search?q=intitle:%EB%8B%A8%EB%8F%85+site:dt.co.kr&hl=ko&gl=KR&ceid=KR:ko", True),
    ("서울경제", "https://news.google.com/rss/search?q=intitle:%EB%8B%A8%EB%8F%85+site:sedaily.com&hl=ko&gl=KR&ceid=KR:ko", True),
    ("머니투데이", "https://rss.mt.co.kr/mt_news.xml", False),
    ("한겨레", "https://www.hani.co.kr/rss/", False),
    ("경향신문", "https://www.khan.co.kr/rss/rssdata/total_news.xml", False),
    ("한국일보", "https://news.google.com/rss/search?q=intitle:%EB%8B%A8%EB%8F%85+site:hankookilbo.com&hl=ko&gl=KR&ceid=KR:ko", True),
    ("뉴스1", "https://news.google.com/rss/search?q=intitle:%EB%8B%A8%EB%8F%85+site:news1.kr&hl=ko&gl=KR&ceid=KR:ko", True),
    # Tier 3: 주요 경제지·IT 전문지·반도체 버티컬
    ("디일렉", "https://www.thelec.kr/rss/allArticle.xml", False),
    ("디지털데일리", "https://news.google.com/rss/search?q=intitle:%EB%8B%A8%EB%8F%85+site:ddaily.co.kr&hl=ko&gl=KR&ceid=KR:ko", True),
    ("지디넷코리아", "https://news.google.com/rss/search?q=intitle:%EB%8B%A8%EB%8F%85+site:zdnet.co.kr&hl=ko&gl=KR&ceid=KR:ko", True),
    ("IT조선", "https://news.google.com/rss/search?q=intitle:%EB%8B%A8%EB%8F%85+site:it.chosun.com&hl=ko&gl=KR&ceid=KR:ko", True),
    ("아이뉴스24", "https://www.inews24.com/rss/news_all.xml", False),
    ("블로터", "https://www.bloter.net/rss/allArticle.xml", False),
    ("이데일리", "http://rss.edaily.co.kr/economy_news.xml", False),
    ("아시아경제", "https://news.google.com/rss/search?q=intitle:%EB%8B%A8%EB%8F%85+site:asiae.co.kr&hl=ko&gl=KR&ceid=KR:ko", True),
    # Tier 4: 테크·인프라·부품 버티컬
    ("바이라인네트워크", "https://byline.network/feed/", False),
    ("테크월드", "https://www.epnc.co.kr/rss/allArticle.xml", False),
    ("데이터넷", "https://www.datanet.co.kr/rss/allArticle.xml", False),
]

# 방식 B: 상장사·기업·자본시장 6대 카테고리 확장 키워드
BIZ_KEYWORDS = [
    # 1. 수주·공급망 모멘텀
    "수주", "공급", "납품", "계약", "발주", "양산", "출하", "퀄테스트", "퀄 통과",
    "벤더", "협력사", "OEM", "ODM", "장기공급", "독점",
    # 2. M&A·지배구조·자금조달
    "인수", "매각", "합병", "M&A", "지분", "경영권", "공개매수", "분할", "물적분할",
    "인적분할", "자회사", "상장", "IPO", "스팩", "유상증자", "유증", "무상증자",
    "CB", "BW", "자사주", "소각", "배당", "행동주의",
    # 3. 설비투자(Capex)·R&D·인허가
    "투자", "증설", "신설", "착공", "가동", "공장", "팹", "라인", "특허",
    "임상", "FDA", "식약처", "품목허가", "기술이전", "라이선스", "MOU", "합작", "JV",
    # 4. 실적·공시·기업 리스크
    "실적", "영업이익", "매출", "적자", "흑자", "어닝", "가이던스", "공시",
    "상장폐지", "상폐", "감자", "횡령", "배임", "압수수색", "과징금", "리콜",
    "파업", "구조조정", "희망퇴직", "워크아웃", "법정관리",
    # 5. 국내 주요 그룹·글로벌 빅테크·핵심 성장 섹터
    "삼성", "SK", "LG", "현대차", "기아", "한화", "HD현대", "포스코", "두산",
    "카카오", "네이버", "NAVER", "셀트리온", "에코프로", "쿠팡", "하이브", "크래프톤", "엔씨",
    "엔비디아", "TSMC", "ASML", "오픈AI", "애플", "테슬라", "마이크로소프트", "구글",
    "아마존", "브로드컴", "AMD", "인텔", "ARM",
    "반도체", "HBM", "파운드리", "소부장", "패키징", "유리기판", "CXL", "데이터센터",
    "AI", "클라우드", "배터리", "2차전지", "전고체", "양극재", "원전", "SMR",
    "전력", "변압기", "전선", "방산", "조선", "우주", "로봇", "휴머노이드",
    "자율주행", "바이오", "신약", "ADC", "비만약",
    # 6. 자본시장·금융 주체
    "국민연금", "사모펀드", "PEF", "MBK", "한앤코", "IMM", "금감원", "금융위", "거래소", "한은", "기준금리",
]

# 스포츠·사회·비기업 오탐 차단 키워드
EXCLUDE_KEYWORDS = [
    "라이온즈", "트윈스", "베어스", "이글스", "자이언츠", "위즈", "나이츠",
    "히어로즈", "랜더스", "다이노스", "프로야구", "KBO", "프로농구", "KBL",
    "프로배구", "V리그", "홈런", "올림픽", "월드컵", "국가대표",
    "조류인플루엔자", "조류독감", "살처분", "임대차", "전세사기",
    "마약", "성폭행", "수능", "학폭", "반론보도", "정정보도",
]

EXCLUSIVE_TAG_RE = re.compile(r"[\[\(][^\]\)]*단독[^\]\)]*[\]\)]")
LEADING_TAG_RE = re.compile(r"^\s*(?:[\[\(][^\]\)]*단독[^\]\)]*[\]\)]\s*)+")


def load_history() -> tuple[list[str], list[str]]:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data.get("links", []), data.get("titles", [])
                if isinstance(data, list):
                    return data, []
        except Exception:
            pass
    return [], []


def save_history(links: list[str], titles: list[str]) -> None:
    data = {
        "links": links[-3000:],
        "titles": titles[-150:],
    }
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_pub_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    s = date_str.strip()
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_rss_items(raw_bytes: bytes) -> list[tuple[str, str, str]]:
    """RSS/Atom XML에서 (title, link, pubDate) 튜플 리스트를 추출합니다."""
    text = raw_bytes.decode("utf-8", errors="replace").lstrip("\ufeff \t\r\n")
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        # 미이스케이프 '&' 문자 보정 후 재파싱
        sanitized = re.sub(r"&(?!#?\w+;)", "&amp;", text)
        try:
            root = ET.fromstring(sanitized.encode("utf-8"))
        except ET.ParseError:
            return []

    items = []
    # RSS <item>
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (
            item.findtext("pubDate")
            or item.findtext("{http://purl.org/dc/elements/1.1/}date")
            or ""
        ).strip()
        if title and link:
            items.append((title, link, pub))

    # Atom <entry>
    if not items:
        ns = "{http://www.w3.org/2005/Atom}"
        for entry in root.findall(f".//{ns}entry"):
            title = (entry.findtext(f"{ns}title") or "").strip()
            link_el = entry.find(f"{ns}link")
            link = (link_el.attrib.get("href", "") if link_el is not None else "").strip()
            pub = (entry.findtext(f"{ns}published") or entry.findtext(f"{ns}updated") or "").strip()
            if title and link:
                items.append((title, link, pub))

    return items


def clean_headline(raw_title: str, is_google_news: bool) -> str:
    title = html.unescape(raw_title).strip()
    if is_google_news:
        title = re.sub(r"\s+-\s+[^-]+$", "", title).strip()
    # 상단 헤더에 '[단독] 언론사명'을 표기하므로 제목 앞 중복 [단독] 태그 제거
    title = LEADING_TAG_RE.sub("", title).strip()
    return title


def extract_tokens(title: str) -> set[str]:
    return set(re.findall(r"[가-힣a-zA-Z0-9]{2,}", title))


def is_duplicate_title(clean_title: str, recent_titles: list[str]) -> bool:
    norm = re.sub(r"\s+", "", clean_title)
    t_tokens = extract_tokens(clean_title)
    for prev in recent_titles[-100:]:
        if re.sub(r"\s+", "", prev) == norm:
            return True
        if len(t_tokens) >= 3:
            p_tokens = extract_tokens(prev)
            if len(p_tokens) >= 3:
                overlap = len(t_tokens & p_tokens) / min(len(t_tokens), len(p_tokens))
                if overlap >= 0.65:
                    return True
    return False


def fetch_single_feed(press: str, rss_url: str, is_gnews: bool, max_age_minutes: int) -> list[dict]:
    results = []
    try:
        resp = requests.get(rss_url, headers=HEADERS, timeout=10, verify=False)
        if resp.status_code != 200:
            return results
        entries = parse_rss_items(resp.content)
    except Exception:
        return results

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(minutes=max_age_minutes)

    for raw_title, link, pub_str in entries:
        if not raw_title or not link:
            continue

        # 1. [단독] / (단독) 태그 필수 확인
        if not EXCLUSIVE_TAG_RE.search(raw_title):
            continue

        # 2. 발행 시각 검증 (과거 기사 도배 차단)
        pub_dt = parse_pub_date(pub_str)
        if pub_dt is not None and pub_dt < cutoff:
            continue

        # 3. 제외 키워드 차단
        if any(ex in raw_title for ex in EXCLUDE_KEYWORDS):
            continue

        # 4. 기업·자본시장 키워드 필터링 (방식 B)
        if not any(kw in raw_title for kw in BIZ_KEYWORDS):
            continue

        cleaned = clean_headline(raw_title, is_gnews)
        if not cleaned:
            continue

        results.append({
            "press": press,
            "title": cleaned,
            "link": link,
            "published_at": pub_dt or now_utc,
        })

    return results


def is_quiet_hours_kst() -> bool:
    hour = datetime.now(KST).hour
    return 0 <= hour < 7


def format_message(press: str, title: str, link: str) -> str:
    safe_press = html.escape(press, quote=False)
    safe_title = html.escape(title, quote=False)
    safe_link = html.escape(link, quote=True)
    return f'<a href="{safe_link}"><b>[단독] {safe_press}</b></a>\n\n{safe_title}'


def send_telegram(press: str, title: str, link: str, dry_run: bool = False) -> bool:
    msg = format_message(press, title, link)
    if dry_run:
        print(f"[DRY-RUN]\n{msg}\n" + "-" * 40)
        return True

    if not TELEGRAM_BOT_TOKEN:
        print("[ERROR] TELEGRAM_BOT_TOKEN 환경변수가 설정되지 않았습니다.")
        return False

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": is_quiet_hours_kst(),
    }

    for attempt in range(3):
        try:
            r = requests.post(api_url, data=payload, timeout=10, verify=False)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                wait_sec = r.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(wait_sec + 1)
            else:
                print(f"[ERROR] Telegram API HTTP {r.status_code}: {r.text[:200]}")
                return False
        except Exception as e:
            print(f"[ERROR] Telegram request failed (attempt {attempt + 1}/3): {e}")
            time.sleep(2)
    return False


def main():
    parser = argparse.ArgumentParser(description="Korean Corporate Exclusive News Telegram Bot")
    parser.add_argument("--dry-run", action="store_true", help="텔레그램 미발송, 콘솔 미리보기만 출력")
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_MINUTES, help="수집 허용 최대 경과 시간(분)")
    args = parser.parse_args()

    sent_links, recent_titles = load_history()
    sent_set = set(sent_links)
    candidates = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(fetch_single_feed, press, url, is_gnews, args.max_age)
            for press, url, is_gnews in RSS_FEEDS
        ]
        for fut in as_completed(futures):
            try:
                candidates.extend(fut.result())
            except Exception:
                pass

    # 오래된 기사부터 최신 순으로 정렬하여 자연스러운 타임라인 발송
    candidates.sort(key=lambda x: x["published_at"])

    sent_count = 0
    for item in candidates:
        if sent_count >= MAX_SEND_PER_RUN:
            break
        if item["link"] in sent_set:
            continue
        if is_duplicate_title(item["title"], recent_titles):
            continue

        if send_telegram(item["press"], item["title"], item["link"], dry_run=args.dry_run):
            sent_links.append(item["link"])
            sent_set.add(item["link"])
            recent_titles.append(item["title"])
            sent_count += 1
            if not args.dry_run:
                time.sleep(0.5)

    print(f"[DONE] scanned={len(RSS_FEEDS)} feeds, matched={len(candidates)}, sent={sent_count}")

    if sent_count > 0 and not args.dry_run:
        save_history(sent_links, recent_titles)


if __name__ == "__main__":
    main()
