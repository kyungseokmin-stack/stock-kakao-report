"""
매일 아침 관심 종목 시세 + 뉴스를 정리한 공개 웹페이지(GitHub Pages)를 만들고,
카카오톡 '나에게 보내기'로 그 링크를 담은 짧은 메시지 한 통만 전송하는 스크립트.

* 보유 수량 / 평단가 / 수익률 / 증권사명은 공개 페이지에 절대 표시하지 않습니다.
  (수량/평단가는 PORTFOLIO_JSON 안에는 있지만, 이 스크립트가 웹페이지를 만들 때
   의도적으로 제외합니다.)

필요한 환경변수 (GitHub Actions Secrets):
  PORTFOLIO_JSON       : 보유 종목 목록 (JSON 텍스트, README 참고)
  KAKAO_REST_API_KEY
  KAKAO_CLIENT_SECRET
  KAKAO_REFRESH_TOKEN
  NAVER_CLIENT_ID
  NAVER_CLIENT_SECRET
  GH_PAT (선택)        : 리프레시 토큰 자동 갱신용
  GITHUB_REPOSITORY    : GitHub Actions가 자동으로 넣어줌 (owner/repo)
"""

import os
import re
import json
import subprocess
from datetime import datetime, timedelta

import requests

REPORT_PATH = os.path.join(os.path.dirname(__file__), "docs", "index.html")


# ----------------------------------------------------------------------------
# 1. 포트폴리오 로드 (파일�v 아니라 Secret에서 읽음 — 저장소에 절대 남짞 않음)
# ---------------------------------------------------------------------------
def load_portfolio():
    raw = os.environ["PORTFOLIO_JSN"]
    return json.loads(raw)


def collect_watchlist(portfolio):
    """모든 계좌의 종목을 중복 없이 하나의 관심종목 목록으로 합침 (계좌 구분 없음)"""
    seen = set()
    items = []
    for account in portfolio.get("accounts", []):
        for item in account.get("domestic", []):
            key = ("domestic", item["ticker"])
            if key in seen:
                continue
            seen.add(key)
            items.append({"name": item["name"], "ticker": item["ticker"], "domestic": True})
        for item in account.get("overseas", []):
            key = ("overseas", item["ticker"])
            if key in seen:
                continue
            seen.add(key)
            items.append({"name": item["name"], "ticker": item["ticker"], "domestic": False})
    return items


# ---------------------------------------------------------------------------
# 2. 가격 조회
# ---------------------------------------------------------------------------
def get_domestic_price(ticker):
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
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period="5d")
    if hist.empty or len(hist) < 2:
        return None
    last_close = float(hist["Close"].iloc[-1])
    prev_close = float(hist["Close"].iloc[-2])
    pct = (last_close - prev_close) / prev_close * 100
    return {"price": last_close, "pct": pct}


# ----------------------------------------------------------------------------
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
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    params = {"query": name, "display": display, "sort": "date"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        res.raise_for_status()
        items = res.json().get("items", [])
        return [clean_html(i["title"]) for i in items]
    except Exception as e:
        print(f"[뉴스 오롘] {name}: {e}")
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
        print(f"[뉴스 오롘] {ticker}: {e}")
        return []


# ---------------------------------------------------------------------------
# 4. 카카오토큰 갱신 / 전송
# ---------------------------------------------------------------------------
def refresh_kakao_token():
    rest_api_key = os.environ["KAKAO_REST_API_KEY"]
    client_secret = os.environ["KAKAO_CLIENT_SECRET"]
    refresh_token = os.environ["KAKAO_REFRESH_TOKEN"]

    res = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": rest_api_key,
            "client_secret": client_secret,
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
    pat = os.environ.get("GH_PAT")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not pat or not repo:
        print("GH_PAT/GITHUB_REPOSITORY 미설정 - 자동 갱신 생략.")
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


def send_kakao_memo(access_token, text):
    url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
    headers = {"Authorization": f"Bearer {access_token}"}
    template = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": text.splitlines()[-1], "mobile_web_url": text.splitlines()[-1]},
    }
    data = {"template_object": json.dumps(template)}
    res = requests.post(url, headers=headers, data=data, timeout=10)
    ok = res.json().get("result_code") == 0
    if not ok:
        print(f"[카카오 전송 실패] {res.text}")
    return ok


# ---------------------------------------------------------------------------
# 5. 공개 페이지 생성 (수량/평단가/증권사명 절대 미포함)
# ---------------------------------------------------------------------------
def build_html(watchlist):
    rows = []
    for w in watchlist:
        if w["domestic"]:
            info = get_domestic_price(w["ticker"])
            news = get_domestic_news(w["name"])
            price_str = f"{info['price']:,}원" if info else "조회 실패"
        else:
            info = get_overseas_price(w["ticker"])
            news = get_overseas_news(w["ticker"])
            price_str = f"${info['price']:.2f}" if info else "조회 실패"
        pct = info["pct"] if info else 0
        color = "#d92626" if pct >= 0 else "#1a63d1"
        sign = "+" if pct >= 0 else ""
        news_html = "".join(f"<li>{n}</li>" for n in news) or "<li>관련 뉴스 없음</li>"
        rows.append(f"""
        <div class="card">
          <div class="row">
            <span class="name">{w['name']}</span>
            <span class="price">{price_str} <span style="color:{color}">({sign}{pct:.2f}%)</span></span>
          </div>
          <ul class="news">{news_html}</ul>
        </div>""")

    today = datetime.now().strftime("%Y-%m-%d")
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>{today} 아침 브리핑</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background:#f5f5f7; margin:0; padding:20px; color:#1c1c1e; }}
  h1 {{ font-size:20px; margin-bottom:4px; }}
  .updated {{ color:#888; font-size:13px; margin-bottom:20px; }}
  .card {{ background:#fff; border-radius:12px; padding:16px; margin-bottom:12px; box-shadow:0 1px 3px rgba(0,0,0,0.08); }}
  .row {{ display:flex; justify-content:space-between; align-items:center; font-weight:600; margin-bottom:8px; gap:12px; }}
  .name {{ font-size:16px; }}
  .price {{ font-size:15px; white-space:nowrap; }}
  .news {{ margin:0; padding-left:18px; font-size:13px; color:#555; line-height:1.6; }}
</style>
</head>
<body>
  <h1>📈 오늘의 관심 종목</h1>
  <div class="updated">{today} 업데이트 · 시세/뉴스만 표시됩니다</div>
  {''.join(rows)}
</body>
</html>"""


# ---------------------------------------------------------------------------
# 6. 메인
# ---------------------------------------------------------------------------
def main():
    portfolio = load_portfolio()
    watchlist = collect_watchlist(portfolio)

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    html = build_html(watchlist)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/")
    pages_url = f"https://{owner}.github.io/{repo}/"

    access_token = refresh_kakao_token()
    today = datetime.now().strftime("%Y-%m-%d")
    text = f"📈 {today} 아침 브리핑이 준비됐어요.\n관심 종목 시세·뉴스 확인하기:\n{pages_url}"
    send_kakao_memo(access_token, text)

    print("완료")


if __name__ == "__main__":
    main()
