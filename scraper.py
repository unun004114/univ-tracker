import os
import re
import time
import random
import string
import sqlite3
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
URL_FILE_PATH = os.path.join(BASE_DIR, 'url.txt')
DB_PATH = os.path.join(BASE_DIR, 'competition.db')
KEYWORDS = ['건축', '공간디자인', '실내디자인', '전통건축', '도시건축', '건축공학', '실내건축', '파사드', '공간']

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS competition_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            univ TEXT,
            type TEXT,
            dept TEXT,
            quota INTEGER,
            apply INTEGER,
            rate REAL
        )
    ''')
    conn.commit()
    conn.close()

def decode_response(res):
    content = res.content
    meta_charset = re.search(rb'charset=["\']?([\w\-]+)', content, re.IGNORECASE)
    if meta_charset:
        charset = meta_charset.group(1).decode('utf-8', errors='ignore').lower()
        if 'euc' in charset or '949' in charset:
            try: return content.decode('euc-kr')
            except UnicodeDecodeError: pass
        elif 'utf' in charset:
            try: return content.decode('utf-8')
            except UnicodeDecodeError: pass

    for enc in ['euc-kr', 'utf-8', 'cp949']:
        try: return content.decode(enc)
        except UnicodeDecodeError: continue
    return content.decode('utf-8', errors='ignore')

def extract_univ_name(soup, target_url):
    title_tag = soup.find('title')
    if title_tag and title_tag.text:
        raw_title = title_tag.text.strip()
        cleaned = raw_title.replace('/', '').replace(' ', '')
        m = re.search(r'([가-힣]{2,12}(?:대학교|대학|교))', cleaned)
        if m: return m.group(1)
            
    for img in soup.find_all('img'):
        alt = img.get('alt', '')
        if '대학교' in alt or (alt.endswith('대') and len(alt) <= 10):
            clean_alt = re.sub(r'(경\/?쟁\/?률|수시|정시|입학|안내|로고|서\/?비\/?스)', '', alt).strip()
            if clean_alt: return clean_alt

    if 'uos' in target_url: return '서울시립대학교'
    elif 'syu' in target_url or 'sahmyook' in target_url: return '삼육대학교'
    elif 'mju' in target_url: return '명지대학교'
    elif 'jinhakapply' in target_url: return '진학사 수집 대학'
    elif 'uwayapply' in target_url: return '유웨이 수집 대학'
    return '실시간 수집 대학'

def parse_html(html_text, target_url):
    soup = BeautifulSoup(html_text, 'html.parser')
    base_univ_name = extract_univ_name(soup, target_url)
    extracted = []
    tables = soup.find_all('table')

    for table in tables:
        caption = table.find('caption')
        caption_text = caption.get_text().strip() if caption else ''
        heading_text = ''
        curr = table
        while curr and not heading_text:
            prev_h = curr.find_previous(['h1', 'h2', 'h3', 'h4', 'h5'])
            if prev_h:
                heading_text = prev_h.get_text().strip()
                break
            curr = curr.parent

        combined_text = f"{caption_text} {heading_text}"
        if any(skip in combined_text for skip in ['전형별 경쟁률', '전형별 현황', '전형별합계', '총계', '계열별']):
            continue

        raw_type = heading_text if heading_text else caption_text
        if not raw_type or len(raw_type) > 50: raw_type = "일반전형"
        clean_type = re.sub(r'(경\/?쟁\/?률\s*현\/?황|경\/?쟁\/?률|현\/?황|안내)', '', raw_type).strip()
        if not clean_type: clean_type = "일반전형"

        rows = table.find_all('tr')
        if not rows: continue

        grid = {}
        for r_idx, tr in enumerate(rows):
            c_idx = 0
            tds = tr.find_all(['td', 'th'])
            for td in tds:
                while (r_idx, c_idx) in grid: c_idx += 1
                r_span = int(td.get('rowspan', 1))
                c_span = int(td.get('colspan', 1))

                td_str_clean = re.sub(r'<br\s*/?>|</p>|<p>', ' ', str(td), flags=re.IGNORECASE)
                txt = re.sub(r'\s+', ' ', BeautifulSoup(td_str_clean, 'html.parser').get_text().strip())

                for r in range(r_span):
                    for c in range(c_span):
                        grid[(r_idx + r, c_idx + c)] = txt
                c_idx += c_span

        if not grid: continue
        max_r = max(r for r, c in grid.keys()) + 1
        max_c = max(c for r, c in grid.keys()) + 1

        for r in range(max_r):
            row_cells = [grid.get((r, c), "") for c in range(max_c)]
            cleaned_row_cells = []
            for cell in row_cells:
                c_str = cell.strip()
                if not c_str: continue
                if not cleaned_row_cells or cleaned_row_cells[-1] != c_str:
                    cleaned_row_cells.append(c_str)

            row_str = " ".join(cleaned_row_cells)
            if any(x in row_str for x in ['소계', '합계', '총계', '전형합계', '계열소계', '계열합계']): continue
            if '모집단위' in row_str and ('지원' in row_str or '경쟁률' in row_str or '모집인원' in row_str): continue
            if not any(kw in row_str for kw in KEYWORDS): continue

            dept_parts, num_parts, rate_found = [], [], None
            for cell in cleaned_row_cells:
                if cell in ['캠퍼스', '단과대학', '학부/학과', '모집단위', '모집인원', '지원인원', '경쟁률', '전형명']: continue

                ratio_match = re.search(r'([\d\.]+)\s*:\s*1', cell)
                if ratio_match:
                    try:
                        rate_found = float(ratio_match.group(1))
                        continue
                    except ValueError: pass

                clean_val = cell.replace(',', '').replace(' ', '')
                if re.match(r'^\d+(\.\d+)?$', clean_val):
                    try:
                        num_parts.append(float(clean_val))
                        continue
                    except ValueError: pass

                if cell not in [base_univ_name, "인문캠퍼스(서울)", "자연캠퍼스(용인)", "인문캠퍼스", "자연캠퍼스", "서울캠퍼스", "천안캠퍼스"]:
                    if not dept_parts or dept_parts[-1] != cell:
                        dept_parts.append(cell)

            if not dept_parts: continue
            dept_full = " ".join([p.strip() for p in dept_parts if p.strip()])
            if not dept_full: continue

            quota, apply, rate = 0, 0, 0.0
            if rate_found is not None: rate = rate_found

            if len(num_parts) >= 2:
                quota = int(num_parts[-2])
                apply = int(num_parts[-1])
                if rate == 0.0 and quota > 0: rate = round(apply / quota, 2)
            elif len(num_parts) == 1:
                if rate == 0.0: rate = float(num_parts[0])
                else: quota = int(num_parts[0])

            extracted.append({
                "univ": base_univ_name,
                "type": clean_type,
                "dept": dept_full,
                "quota": quota,
                "apply": apply,
                "rate": rate
            })
    return extracted

def fast_scrape_single_url(url):
    data = []
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        res = session.get(url, timeout=10)
        main_html = decode_response(res)
        data.extend(parse_html(main_html, url))

        soup = BeautifulSoup(main_html, 'html.parser')
        for iframe in soup.find_all('iframe'):
            src = iframe.get('src')
            if src:
                try:
                    f_res = session.get(urljoin(url, src), timeout=10)
                    data.extend(parse_html(decode_response(f_res), url))
                except Exception: continue
    except Exception as e:
        print(f"[수집 오류] {url}: {e}")

    unique_data, seen = [], set()
    for item in data:
        key = (item['univ'], item['type'], item['dept'], item['quota'], item['apply'], item['rate'])
        if key not in seen:
            seen.add(key)
            unique_data.append(item)
    return unique_data

def get_urls_from_file():
    if not os.path.exists(URL_FILE_PATH): return []
    with open(URL_FILE_PATH, 'r', encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip() and not line.startswith('#')]

def run_scraping_and_save():
    init_db()
    urls = get_urls_from_file()
    if not urls:
        print("[알림] 등록된 URL이 없어 수집을 하지 않습니다.")
        return

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 수집 시작 (대상 URL: {len(urls)}개)")
    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(fast_scrape_single_url, url) for url in urls]
        for future in futures:
            results.extend(future.result())

    if results:
        # 분 단위 타임스탬프 저장 (10분 단위 비교용)
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:00')
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        for item in results:
            cursor.execute('''
                INSERT INTO competition_history (timestamp, univ, type, dept, quota, apply, rate)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (now_str, item['univ'], item['type'], item['dept'], item['quota'], item['apply'], item['rate']))
        conn.commit()
        conn.close()
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 성공: {len(results)}건 DB 기록 저장 완료.")