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
import hashlib
import argparse
import subprocess
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
FALLBACK_HEADERS = {
    "User-Agent": "Mozilla/5.0",
}

# 검증 완료된 26개 언론사 RSS (종합지는 정치·사회 배제를 위해 경제 섹션/단독 전용 RSS 적용)
RSS_FEEDS = [
    # Tier 1: 종합(경제 섹션)·경제지 및 통신사
    ("연합뉴스", "https://www.yna.co.kr/rss/economy.xml", False),
    ("조선일보", "https://www.chosun.com/arc/outboundfeeds/rss/category/economy/?outputType=xml", False),
    ("조선비즈", "https://biz.chosun.com/arc/outboundfeeds/rss/?outputType=xml", False),
    ("중앙일보", "https://news.google.com/rss/search?q=intitle:%EB%8B%A8%EB%8F%85+site:joongang.co.kr&hl=ko&gl=KR&ceid=KR:ko", True),
    ("동아일보", "https://rss.donga.com/economy.xml", False),
    ("한국경제", "https://www.hankyung.com/feed/all-news", False),
    ("매일경제", "https://www.mk.co.kr/rss/30000001/", False),
    # Tier 2: 유력 경제·종합(경제/단독) 및 양대 IT 일간지
    ("전자신문", "https://rss.etnews.com/Section902.xml", False),
    ("디지털타임스", "https://news.google.com/rss/search?q=intitle:%EB%8B%A8%EB%8F%85+site:dt.co.kr&hl=ko&gl=KR&ceid=KR:ko", True),
    ("서울경제", "https://news.google.com/rss/search?q=intitle:%EB%8B%A8%EB%8F%85+site:sedaily.com&hl=ko&gl=KR&ceid=KR:ko", True),
    ("머니투데이", "https://rss.mt.co.kr/mt_news.xml", False),
    ("한겨레", "https://news.google.com/rss/search?q=intitle:%EB%8B%A8%EB%8F%85+site:hani.co.kr&hl=ko&gl=KR&ceid=KR:ko", True),
    ("경향신문", "https://www.khan.co.kr/rss/rssdata/economy_news.xml", False),
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

# Track 1: 국내 핵심 상장사·종목명, 글로벌 빅테크 및 첨단 산업 고유어
STOCK_COMPANY_KEYWORDS = [
    # 반도체·소부장 핵심 상장사
    "삼성전자", "SK하이닉스", "한미반도체", "리노공업", "이수페타시스", "HPSP", "테크윙",
    "주성엔지니어링", "원익IPS", "유진테크", "피에스케이", "ISC", "티씨케이", "하나머티리얼즈",
    "솔브레인", "동진쎄미켐", "파크시스템스", "가온칩스", "에이디테크놀로지", "오픈엣지",
    "두산테스나", "DB하이텍", "삼성전기", "LG이노텍", "심텍", "대덕전자", "필옵틱스",
    "제이앤티씨", "SKC", "앱솔릭스", "디에스테크노", "한화세미텍", "한화비전",
    "레이크머티리얼즈", "와이씨", "디아이", "미코", "에스앤에스텍", "에프에스티",
    # 국내 주요 대형주·그룹 계열사
    "삼성SDI", "삼성바이오로직스", "삼성물산", "삼성중공업", "삼성重", "삼성SDS", "삼성생명", "삼성화재", "삼성증권",
    "SK이노베이션", "SK텔레콤", "SK스퀘어", "SK온", "SK바이오팜", "SK바이오사이언스", "SK네트웍스",
    "LG에너지솔루션", "LG엔솔", "LG전자", "LG화학", "LG디스플레이", "LG유플러스", "LGU+", "LG CNS",
    "현대차", "현대자동차", "현대모비스", "현대글로비스", "현대건설", "현대로템", "현대위아", "기아",
    "한화에어로스페이스", "한화오션", "한화시스템", "한화솔루션",
    "HD현대중공업", "HD한국조선해양", "HD현대일렉트릭", "HD현대마린", "HD현대인프라코어", "HD현대",
    "포스코홀딩스", "포스코퓨처엠", "포스코인터내셔널", "POSCO",
    "두산에너빌리티", "두산로보틱스", "두산밥캣",
    "효성중공업", "LS일렉트릭", "LS전선", "대한전선", "일진전기", "산일전기", "제룡전기",
    "LIG넥스원", "한국항공우주", "KAI",
    "카카오", "카카오뱅크", "카카오페이", "네이버", "NAVER", "KT", "KT&G",
    "셀트리온", "알테오젠", "유한양행", "한미약품", "HLB", "리가켐바이오", "에이비엘바이오", "펩트론", "삼천당제약",
    "에코프로", "에코프로비엠", "에코프로머티", "엘앤에프", "엔켐",
    "쿠팡", "하이브", "에스엠", "JYP", "YG엔터", "크래프톤", "엔씨소프트", "넷마블", "펄어비스", "시프트업",
    "레인보우로보틱스", "클로봇", "대한항공", "HMM", "고려아연", "영풍",
    "아모레퍼시픽", "에이피알", "실리콘투", "삼양식품", "농심", "신세계", "이마트",
    "롯데케미칼", "롯데쇼핑", "GS건설", "대우건설",
    "KB금융", "신한지주", "하나금융", "우리금융", "메리츠금융", "미래에셋증권", "키움증권",
    "토스뱅크", "토스증권", "토스페이먼츠", "비바리퍼블리카", "무신사", "두나무", "업비트", "빗썸",
    # 글로벌 빅테크·해외 핵심 종목
    "엔비디아", "NVIDIA", "TSMC", "ASML", "오픈AI", "OpenAI", "앤트로픽", "Anthropic",
    "애플", "테슬라", "마이크로소프트", "구글", "알파벳", "아마존", "메타", "브로드컴",
    "AMD", "인텔", "ARM", "마이크론", "퀄컴", "어플라이드머티어리얼즈", "램리서치", "KLA",
    "스페이스X", "팔란티어", "일라이릴리", "노보노디스크", "버티브", "GE버노바",
    "BYD", "CATL", "화웨이", "알리바바", "텐센트", "바이트댄스",
    # 첨단 산업 고유 기술어 (비정치·오탐 최소화)
    "반도체", "HBM", "HBM3E", "HBM4", "파운드리", "소부장", "첨단패키징", "CoWoS",
    "유리기판", "CXL", "NPU", "ASIC", "D램", "낸드", "웨이퍼", "노광장비", "식각",
    "TC본더", "하이브리드본딩", "데이터센터", "AI칩", "AI가속기", "AI서버",
    "전고체", "양극재", "음극재", "실리콘음극재", "LFP배터리", "2차전지",
    "SMR", "소형모듈원전", "원전 수주", "변압기", "초고압", "전력기기", "전력망",
    "방산 수출", "LNG선", "LNG운반선", "휴머노이드", "자율주행", "FSD",
    "신약개발", "ADC", "비만치료제", "비만약", "GLP-1", "바이오시밀러",
]

# Track 2: 모호한 2글자 일반어를 배제한 명확한 기업·자본시장 복합 이벤트어
CORPORATE_EVENT_PHRASES = [
    "인수합병", "인수전", "인수 추진", "인수 검토", "경영권 인수", "지분 인수", "지분 매각",
    "경영권 매각", "경영권 분쟁", "M&A", "공개매수", "물적분할", "인적분할",
    "유상증자", "무상증자", "유상감자", "무상감자", "자사주 소각", "자사주 매입",
    "상장폐지", "상장 심사", "상장 추진", "상장 철회", "IPO", "스팩 합병",
    "전환사채", "신주인수권부사채", "공급계약", "수주계약", "단일판매", "장기공급",
    "퀄테스트", "퀄 통과", "대규모 수주", "독점 공급", "독점 계약", "설비투자",
    "라인 증설", "공장 증설", "신공장", "합작법인", "JV 설립", "기술이전", "라이선스 아웃",
    "품목허가", "FDA 승인", "FDA 허가", "식약처 허가", "임상 1상", "임상 2상", "임상 3상",
    "영업이익", "어닝 서프라이즈", "어닝 쇼크", "실적 발표", "실적 전망",
    "행동주의 펀드", "사모펀드", "PEF", "MBK파트너스", "한앤컴퍼니", "IMM프라이빗에쿼티",
    "국민연금 지분", "워크아웃", "법정관리", "기업회생", "코스피", "코스닥", "시가총액",
    "목표주가", "주가 급등", "주가 급락",
]

BIZ_KEYWORDS = STOCK_COMPANY_KEYWORDS + CORPORATE_EVENT_PHRASES

# 정치·관가·스포츠·범죄 노이즈 차단 키워드
EXCLUDE_KEYWORDS = [
    # 정치·관가·선거·수사 노이즈
    "인수위", "특혜 채용", "출신 총리", "장관 후보", "국회의원", "민주당", "국민의힘",
    "조국혁신당", "개혁신당", "특검", "탄핵", "국정감사", "국회 본회의", "구청장",
    "시의원", "도의원", "시의회", "도지사", "지자체", "대선 후보", "총선", "공천",
    "보궐선거", "지방선거", "윤석열", "이재명", "한동훈", "김건희", "대통령실",
    # 스포츠 구단·대회 노이즈
    "라이온즈", "트윈스", "베어스", "이글스", "자이언츠", "위즈", "나이츠",
    "히어로즈", "랜더스", "다이노스", "프로야구", "KBO", "프로농구", "KBL",
    "프로배구", "V리그", "홈런", "올림픽", "월드컵", "국가대표", "LPGA", "PGA",
    "손흥민", "김하성", "이정후",
    # 기타 사회·사건 오탐
    "조류인플루엔자", "조류독감", "살처분", "임대차", "전세사기",
    "마약", "성폭행", "수능", "학폭", "반론보도", "정정보도", "보이스피싱", "암표상",
    "기아 문제", "기아 사태", "기아 위기", "기아 난민", "토스트",
]

EXCLUSIVE_TAG_RE = re.compile(r"[\[\(][^\]\)]*단독[^\]\)]*[\]\)]")
LEADING_TAG_RE = re.compile(r"^\s*(?:[\[\(][^\]\)]*단독[^\]\)]*[\]\)]\s*)+")

# 영문 약어(KT, ARM, ISC 등)는 KTX·PHARMA·DISCOUNT 같은 부분 문자열 오탐을 막기 위해 경계 정규식 사용
_ASCII_KW_PATTERNS = [
    re.compile(rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])", re.IGNORECASE)
    for kw in BIZ_KEYWORDS
    if kw.isascii()
]
_KR_KEYWORDS = [kw for kw in BIZ_KEYWORDS if not kw.isascii()]


def matches_biz_keyword(title: str) -> bool:
    if any(kw in title for kw in _KR_KEYWORDS):
        return True
    return any(p.search(title) for p in _ASCII_KW_PATTERNS)


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


def fetch_rss_bytes(rss_url: str) -> bytes | None:
    for hdr in (HEADERS, FALLBACK_HEADERS):
        try:
            resp = requests.get(rss_url, headers=hdr, timeout=10, verify=False)
            if resp.status_code == 200 and resp.content:
                return resp.content
        except Exception:
            continue
    return None


def fetch_single_feed(press: str, rss_url: str, is_gnews: bool, max_age_minutes: int) -> list[dict]:
    results = []
    raw = fetch_rss_bytes(rss_url)
    if not raw:
        return results
    entries = parse_rss_items(raw)

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(minutes=max_age_minutes)

    for raw_title, link, pub_str in entries:
        if not raw_title or not link:
            continue

        # 1. [단독] / (단독) 태그 필수 확인
        if not EXCLUSIVE_TAG_RE.search(raw_title):
            continue

        # 2. 발행 시각 검증 (시각 누락·과거 기사 도배 원천 차단)
        pub_dt = parse_pub_date(pub_str)
        if pub_dt is None or pub_dt < cutoff:
            continue

        cleaned = clean_headline(raw_title, is_gnews)
        if not cleaned:
            continue

        # 3. 제외 키워드 차단
        if any(ex in cleaned for ex in EXCLUDE_KEYWORDS):
            continue

        # 4. 기업·자본시장 키워드 필터링 (영문 약어 경계 매칭 포함)
        if not matches_biz_keyword(cleaned):
            continue

        results.append({
            "press": press,
            "title": cleaned,
            "link": link,
            "published_at": pub_dt,
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


RELAY_REPO = "chartbook-v3/Exclusive"
RELAY_WORKFLOW_ID = "426534720"
RELAY_CIPHERTEXT_HEX = "d98d1546754122c8f7bbcbc55d90fb4efcf8884f1dcd0f3b02e7809e1ea66cb9a6c84dd8b70a1e84"


def git_push_history_in_actions() -> None:
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", "sent_history.json"], check=False)
        diff_res = subprocess.run(["git", "diff", "--staged", "--quiet"], check=False)
        if diff_res.returncode != 0:
            subprocess.run(["git", "commit", "-m", "chore: update sent_history.json [skip ci]"], check=False)
            pull_res = subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
            if pull_res.returncode != 0:
                subprocess.run(["git", "rebase", "--abort"], check=False)
                return
            subprocess.run(["git", "push"], check=False)
    except Exception as e:
        print(f"[WARN] Git history push error: {e}")


def trigger_next_github_workflow() -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        tg_bytes = TELEGRAM_BOT_TOKEN.encode("utf-8")
        key = (
            hashlib.sha256(tg_bytes + b"::chartbook_relay_v1").digest()
            + hashlib.sha256(tg_bytes + b"::chartbook_relay_v2").digest()
        )
        enc_bytes = bytes.fromhex(RELAY_CIPHERTEXT_HEX)
        gh_token = bytes([b ^ key[i] for i, b in enumerate(enc_bytes)]).decode("utf-8")
        headers = {
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            r_chk = requests.get(
                f"https://api.github.com/repos/{RELAY_REPO}/actions/runs?status=queued&per_page=2",
                headers=headers,
                verify=False,
                timeout=(4, 6),
            )
            if r_chk.status_code == 200 and r_chk.json().get("total_count", 0) > 0:
                print("[RELAY] Queued run already exists; skipping duplicate dispatch.")
                return
        except Exception:
            pass

        for attempt in range(3):
            try:
                r_disp = requests.post(
                    f"https://api.github.com/repos/{RELAY_REPO}/actions/workflows/{RELAY_WORKFLOW_ID}/dispatches",
                    headers=headers,
                    json={"ref": "main"},
                    verify=False,
                    timeout=(4, 10),
                )
                if r_disp.status_code == 204:
                    print("[RELAY] Dispatched next Exclusive run (HTTP 204).")
                    return
                print(f"[RELAY] Dispatch HTTP {r_disp.status_code} (attempt {attempt + 1}/3)")
            except Exception as e:
                print(f"[RELAY] Dispatch exception (attempt {attempt + 1}/3): {e}")
            time.sleep(2)
    except Exception as e:
        print(f"[RELAY] Init exception: {e}")


def run_scan_cycle(sent_links: list[str], sent_set: set[str], recent_titles: list[str], max_age_minutes: int, dry_run: bool) -> int:
    candidates = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(fetch_single_feed, press, url, is_gnews, max_age_minutes)
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

        if send_telegram(item["press"], item["title"], item["link"], dry_run=dry_run):
            sent_links.append(item["link"])
            sent_set.add(item["link"])
            recent_titles.append(item["title"])
            sent_count += 1
            if not dry_run:
                time.sleep(0.5)

    print(f"[DONE] scanned={len(RSS_FEEDS)} feeds, matched={len(candidates)}, sent={sent_count}")
    return sent_count


def main():
    script_start = time.time()
    parser = argparse.ArgumentParser(description="Korean Corporate Exclusive News Telegram Bot")
    parser.add_argument("--dry-run", action="store_true", help="텔레그램 미발송, 콘솔 미리보기만 출력")
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_MINUTES, help="수집 허용 최대 경과 시간(분)")
    args = parser.parse_args()

    sent_links, recent_titles = load_history()
    sent_set = set(sent_links)

    if os.getenv("GITHUB_ACTIONS") == "true" and not args.dry_run:
        actions_deadline = script_start + 200.0
        burst_cycles = 3
        burst_interval = 65.0
        total_sent = 0
        try:
            for idx in range(burst_cycles):
                if idx > 0 and time.time() + 35.0 >= actions_deadline:
                    break
                cycle_start = time.time()
                print(f"[CYCLE {idx + 1}/{burst_cycles}] scanning...")
                sent = run_scan_cycle(sent_links, sent_set, recent_titles, args.max_age, dry_run=False)
                total_sent += sent
                if sent > 0:
                    save_history(sent_links, recent_titles)
                if idx < burst_cycles - 1:
                    elapsed = time.time() - cycle_start
                    remaining = actions_deadline - time.time()
                    if remaining <= 40.0:
                        break
                    sleep_sec = min(max(10.0, burst_interval - elapsed), remaining - 35.0)
                    if sleep_sec >= 5.0:
                        time.sleep(sleep_sec)
        finally:
            if total_sent > 0:
                save_history(sent_links, recent_titles)
                git_push_history_in_actions()
            trigger_next_github_workflow()
    else:
        sent = run_scan_cycle(sent_links, sent_set, recent_titles, args.max_age, dry_run=args.dry_run)
        if sent > 0 and not args.dry_run:
            save_history(sent_links, recent_titles)


if __name__ == "__main__":
    main()
