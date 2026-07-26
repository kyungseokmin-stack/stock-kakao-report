"""
매일 아침 포트폴리오 가격 + 뉴스를 카카오톡 '나에게 보내기'로 전송하는 스크립트.

필요한 환경변수 (GitHub Actions Secrets 또는 로컬 .env):
  KAKAO_REST_API_KEY   : 카카오 디벨로퍼스 REST API 키
  KAKAO_REFRESH_TOKEN  : 최초 1회 수동 발급받은 리프레시 토큰 (README 참고)
  NAVER_CLIENT_ID      : 네이버 오픈API 클라이언트 ID
  NAVER_CLIENT_SECRET  : 네이버 오픈API 클라이언트 시크릿
  GH_PAT (선택)        : 리프레시 토큰이 갱신될 때 GitHub Secret을 자동으로
                         업데이트하려면 repo 권한이 있는 Personal Access Token 필요
"""

import os
import re
import json
import time
import subprocess
from datetime import datetime, timedelta

import requests

PORTFOLIO_PATH = os.path.join(os.path.dirname(__file__), "portfolio.json")
KAKAO_TEXT_LIMIT = 190  # 카카오 text 템플릿 200자 제한에 여유를 둠


# ---------------------------------------------------------------------------
# 1. 포트폴리오 로드
# ---------------------------------------------------------------------------
def load_portfolio():
    with open(PORTFOLIO_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 2. 가격 조회
# ---------------------------------------------------------------------------
def get_domestic_price(ticker):
    """pykrx로 최근 종가/전일대비 등락률 조회"""
    from pykrx import stock

    today = datetime.now()
    fromdate = (today - timedelta(days=15)).strftime("%Y%m%d")
    todate = today.strftime("%Y%m%d")

    df = stock.get_market_ohlcv_by_date(fromdate, todate, ticker)
    df = df[df["종가"] > 0]
    if len(df) < 2:
        return None
    last_close = int(df["종가"].iloc[-1])
    prev_close = int(df["종가"].iloc[-2])
    pct = (last_close - prev_close) / prev_close * 100
    return {"price": last_close, "pct": pct}


def get_overseas_price(ticker):
    """yfinance로 최근 종가/전일대비 등락률 조회"""
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period="5d")
    if hist.empty or len(hist) < 2:
        return None
    last_close = float(hist["Close"].iloc[-1])
    prev_close = float(hist["Close"].iloc[-2])
    pct = (last_close - prev_close) / prev_close * 100
    return {"price": last_close, "pct": pct}


# ---------------------------------------------------------------------------
# 3. 뉴스 조회
# ---------------------------------------------------------------------------
def clean_html(text):
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&quot;", '"').replace("&amp;", "&").replace("&#39;", "'")


def get_domestic_news(name, display=2):
    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        return []

    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    params = {"query": name, "display": display, "sort": "date"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        res.raise_for_status()
        items = res.json().get("items", [])
        return [clean_html(i["title"]) for i in items]
    except Exception as e:
        print(f"[뉴스 오류] {name}: {e}")
        return []


def get_overseas_news(ticker, count=2):
    import yfinance as yf

    try:
        news = yf.Ticker(ticker).news or []
        titles = []
        for n in news[:count]:
            title = n.get("title") or n.get("content", {}).get("title")
            if title:
                titles.append(title)
        return titles
    except Exception as e:
        print(f"[뉴스 오류] {ticker}: {e}")
        return []


# ---------------------------------------------------------------------------
# 4. 카카오 토큰 갱신
# ---------------------------------------------------------------------------
def refresh_kakao_token():
    rest_api_key = os.environ["KAKAO_REST_API_KEY"]
    refresh_token = os.environ["KAKAO_REFRESH_TOKEN"]

    res = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": rest_api_key,
            "refresh_token": refresh_token,
        },
        timeout=10,
    )
    res.raise_for_status()
    data = res.json()
    access_token = data["access_token"]
    new_refresh_token = data.get("refresh_token")

    if new_refresh_token:
        print("리프레시 토큰이 갱신되었습니다. Secret 업데이트를 시도합니다.")
        update_github_secret("KAKAO_REFRESH_TOKEN", new_refresh_token)

    return access_token


def update_github_secret(name, value):
    """GH_PAT이 설정돼 있으면 gh CLI로 저장소 Secret을 자동 갱신 (선택 기능)"""
    pat = os.environ.get("GH_PAT")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not pat or not repo:
        print("GH_PAT/GITHUB_REPOSITORY 미설정 - 자동 갱신 생략. "
              "약 2개월 내 refresh token 만료 전 재인증 필요할 수 있음.")
        return
    try:
        env = os.environ.copy()
        env["GH_TOKEN"] = pat
        subprocess.run(
            ["gh", "secret", "set", name, "--repo", repo, "--body", value],
            env=env, check=True,
        )
        print(f"{name} Secret 자동 갱신 완료")
    except Exception as e:
        print(f"[Secret 갱신 실패] {e}")


# ---------------------------------------------------------------------------
# 5. 카카오 메시지 전송 (200자 제한 -> 여러 통으로 분할)
# ---------------------------------------------------------------------------
def chunk_text(lines, limit=KAKAO_TEXT_LIMIT):
    chunks = []
    current = ""
    for line in lines:
        candidate = (current + "\n" + line) if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_kakao_memo(access_token, text):
    url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
    headers = {"Authorization": f"Bearer {access_token}"}
    template = {"object_type": "text", "text": text,
                "link": {"web_url": "https://finance.naver.com", "mobile_web_url": "https://finance.naver.com"}}
    data = {"template_object": json.dumps(template)}
    res = requests.post(url, headers=headers, data=data, timeout=10)
    ok = res.json().get("result_code") == 0
    if not ok:
        print(f"[카카오 전송 실패] {res.text}")
    return ok


# ---------------------------------------------------------------------------
# 6. 메인 로직
# ---------------------------------------------------------------------------
def build_domestic_lines(items):
    lines = ["[국내]"]
    for item in items:
        price_info = get_domestic_price(item["ticker"])
        if not price_info:
            lines.append(f"{item['name']}: 가격 조회 실패")
            continue
        sign = "+" if price_info["pct"] >= 0 else ""
        line = f"{item['name']} {price_info['price']:,}원 ({sign}{price_info['pct']:.2f}%)"
        if item.get("quantity") and item.get("avg_price"):
            profit_pct = (price_info["price"] - item["avg_price"]) / item["avg_price"] * 100
            line += f" | 수익률 {profit_pct:+.2f}%"
        lines.append(line)
        for news in get_domestic_news(item["name"]):
            lines.append(f"  - {news}")
    return lines


def build_overseas_lines(items):
    lines = ["[해외]"]
    for item in items:
        price_info = get_overseas_price(item["ticker"])
        if not price_info:
            lines.append(f"{item['name']}: 가격 조회 실패")
            continue
        sign = "+" if price_info["pct"] >= 0 else ""
        line = f"{item['name']} ${price_info['price']:.2f} ({sign}{price_info['pct']:.2f}%)"
        if item.get("quantity") and item.get("avg_price"):
            profit_pct = (price_info["price"] - item["avg_price"]) / item["avg_price"] * 100
            line += f" | 수익률 {profit_pct:+.2f}%"
        if item.get("daily_buy_krw"):
            line += f" | 매일 {item['daily_buy_krw']:,}원 자동매수"
        lines.append(line)
        for news in get_overseas_news(item["ticker"]):
            lines.append(f"  - {news}")
    return lines


def build_report_lines(portfolio):
    lines = [f"📈 {datetime.now().strftime('%Y-%m-%d')} 아침 포트폴리오 브리핑"]

    for account in portfolio.get("accounts", []):
        broker = account.get("broker", "계좌")
        lines.append(f"\n■ {broker}")

        domestic = account.get("domestic", [])
        if domestic:
            lines.append("")
            lines.extend(build_domestic_lines(domestic))

        overseas = account.get("overseas", [])
        if overseas:
            lines.append("")
            lines.extend(build_overseas_lines(overseas))

    return lines


def main():
    portfolio = load_portfolio()
    lines = build_report_lines(portfolio)
    chunks = chunk_text(lines)

    access_token = refresh_kakao_token()

    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        prefix = f"({idx}/{total})\n" if total > 1 else ""
        send_kakao_memo(access_token, prefix + chunk)
        time.sleep(1)

    print("완료")


if __name__ == "__main__":
    main()
