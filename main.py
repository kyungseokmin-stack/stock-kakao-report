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


# ---------------------------------------------------------------------------
# 1. 포트폴리오 로드 (파일이 아니라 Secret에서 읽음 — 저장소에 절대 남지 않음)
# ---------------------------------------------------------------------------
def load_portfolio():
    raw = os.environ["PORTFOLIO_JSON"]
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
# 2. 가격 + 기술적 지표 조회
# ---------------------------------------------------------------------------
def _bollinger(close, window=20, n_std=2):
    mid = close.rolling(window).mean().iloc[-1]
    std = close.rolling(window).std().iloc[-1]
    upper = mid + n_std * std
    lower = mid - n_std * std
    return mid, upper, lower


def _ichimoku_cloud_position(high, low, close):
    """전환선/기준선 기반 구름대 위치 (단순화 버전)"""
    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
    span_b = (high.rolling(52).max() + low.rolling(52).min()) / 2
    if len(close) < 52:
        return None
    span_a_now = (tenkan.iloc[-1] + kijun.iloc[-1]) / 2
    span_b_now = span_b.iloc[-1]
    cloud_top = max(span_a_now, span_b_now)
    cloud_bottom = min(span_a_now, span_b_now)
    last = close.iloc[-1]
    if last > cloud_top:
        position = "구름대 위"
    elif last < cloud_bottom:
        position = "구름대 아래"
    else:
        position = "구름대 안"
    return {
        "tenkan": tenkan.iloc[-1],
        "kijun": kijun.iloc[-1],
        "position": position,
    }


def _heavy_volume_zone(df, close_col, volume_col, lookback=60, bins=10):
    """최근 lookback일 동안 거래량이 가장 많이 쌓인 가격대(매물대) 추정"""
    try:
        import pandas as pd

        recent = df.tail(lookback)
        if len(recent) < 10:
            return None
        price_bins = pd.cut(recent[close_col], bins=bins)
        vol_by_bin = recent.groupby(price_bins, observed=True)[volume_col].sum()
        if vol_by_bin.empty or vol_by_bin.max() == 0:
            return None
        top = vol_by_bin.idxmax()
        return {"low": top.left, "high": top.right}
    except Exception as e:
        print(f"[매물대 계산 오류] {e}")
        return None


# ---------------------------------------------------------------------------
# 2-1. 규칙 기반 관찰 알림 엔진
#      * 매수/매도 신호가 아니라 "이런 차트 조건이 충족됐다"는 사실만 표시합니다.
#      * 새 규칙을 추가하려면 아래 ALERT_RULES 리스트에 함수만 더하면 됩니다.
#        각 규칙 함수는 (close, volume) 시계열(pandas Series)을 받아
#        조건 충족 시 {"label": ..., "detail": ...} 딕셔너리를, 아니면 None을 반환합니다.
# ---------------------------------------------------------------------------
import pandas as pd


def rule_volume_surge_at_lower_band(close, volume):
    """오늘 거래량이 최근 3일 평균의 2배 이상 + 전일 종가가 볼린저밴드 하단 이하"""
    if len(close) < 22 or len(volume) < 4:
        return None
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    lower = mid - 2 * std

    vol_avg3 = volume.iloc[-4:-1].mean()  # 오늘을 제외한 최근 3일 평균
    vol_today = volume.iloc[-1]
    prev_close = close.iloc[-2]
    prev_lower = lower.iloc[-2]

    if vol_avg3 <= 0 or pd.isna(prev_lower):
        return None

    vol_ratio = vol_today / vol_avg3
    if vol_ratio >= 2 and prev_close <= prev_lower:
        return {
            "label": "상승 모니터링",
            "detail": f"거래량이 최근 3일 평균의 {vol_ratio:.1f}배 + 전일 볼린저밴드 하단 터치",
        }
    return None


def rule_volume_surge_at_upper_band(close, volume):
    """오늘 거래량이 최근 3일 평균의 2배 이상 + 전일 종가가 볼린저밴드 상단 이상"""
    if len(close) < 22 or len(volume) < 4:
        return None
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    upper = mid + 2 * std

    vol_avg3 = volume.iloc[-4:-1].mean()
    vol_today = volume.iloc[-1]
    prev_close = close.iloc[-2]
    prev_upper = upper.iloc[-2]

    if vol_avg3 <= 0 or pd.isna(prev_upper):
        return None

    vol_ratio = vol_today / vol_avg3
    if vol_ratio >= 2 and prev_close >= prev_upper:
        return {
            "label": "과열 모니터링",
            "detail": f"거래량이 최근 3일 평균의 {vol_ratio:.1f}배 + 전일 볼린저밴드 상단 터치",
        }
    return None


def _rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    # avg_loss가 0인데 avg_gain > 0이면 (계속 상승만) RSI = 100
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain > 0)), 100)
    # avg_gain, avg_loss 둘 다 0이면 (변동 없음) RSI = 50
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain == 0)), 50)
    return rsi


def _macd(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def _cross_signal(short_ma, long_ma):
    """short_ma가 long_ma를 어제→오늘 사이 상향(1)/하향(-1) 돌파했는지, 아니면 0"""
    if len(short_ma) < 2 or pd.isna(short_ma.iloc[-2]) or pd.isna(long_ma.iloc[-2]):
        return 0
    prev_diff = short_ma.iloc[-2] - long_ma.iloc[-2]
    today_diff = short_ma.iloc[-1] - long_ma.iloc[-1]
    if pd.isna(prev_diff) or pd.isna(today_diff):
        return 0
    if prev_diff <= 0 and today_diff > 0:
        return 1
    if prev_diff >= 0 and today_diff < 0:
        return -1
    return 0


def rule_golden_cross_short(close, volume):
    """5일 이동평균이 20일 이동평균을 상향 돌파 (단기 골든크로스)"""
    if len(close) < 21:
        return None
    signal = _cross_signal(close.rolling(5).mean(), close.rolling(20).mean())
    if signal == 1:
        return {"label": "골든크로스(단기)", "detail": "5일 이동평균이 20일 이동평균을 상향 돌파"}
    return None


def rule_dead_cross_short(close, volume):
    """5일 이동평균이 20일 이동평균을 하향 돌파 (단기 데드크로스)"""
    if len(close) < 21:
        return None
    signal = _cross_signal(close.rolling(5).mean(), close.rolling(20).mean())
    if signal == -1:
        return {"label": "데드크로스(단기)", "detail": "5일 이동평균이 20일 이동평균을 하향 돌파"}
    return None


def rule_golden_cross_mid(close, volume):
    """20일 이동평균이 60일 이동평균을 상향 돌파 (중기 골든크로스)"""
    if len(close) < 61:
        return None
    signal = _cross_signal(close.rolling(20).mean(), close.rolling(60).mean())
    if signal == 1:
        return {"label": "골든크로스(중기)", "detail": "20일 이동평균이 60일 이동평균을 상향 돌파"}
    return None


def rule_dead_cross_mid(close, volume):
    """20일 이동평균이 60일 이동평균을 하향 돌파 (중기 데드크로스)"""
    if len(close) < 61:
        return None
    signal = _cross_signal(close.rolling(20).mean(), close.rolling(60).mean())
    if signal == -1:
        return {"label": "데드크로스(중기)", "detail": "20일 이동평균이 60일 이동평균을 하향 돌파"}
    return None


def rule_rsi_overbought(close, volume):
    """RSI(14)가 70 이상 (과매수 구간)"""
    if len(close) < 15:
        return None
    val = _rsi(close).iloc[-1]
    if pd.isna(val):
        return None
    if val >= 70:
        return {"label": "과매수 모니터링", "detail": f"RSI(14) {float(val):.1f} (70 이상)"}
    return None


def rule_rsi_oversold(close, volume):
    """RSI(14)가 30 이하 (과매도 구간)"""
    if len(close) < 15:
        return None
    val = _rsi(close).iloc[-1]
    if pd.isna(val):
        return None
    if val <= 30:
        return {"label": "과매도 모니터링", "detail": f"RSI(14) {float(val):.1f} (30 이하)"}
    return None


def rule_macd_golden_cross(close, volume):
    """MACD선이 시그널선을 상향 돌파"""
    if len(close) < 35:
        return None
    macd_line, signal_line = _macd(close)
    signal = _cross_signal(macd_line, signal_line)
    if signal == 1:
        return {"label": "MACD 골든크로스", "detail": "MACD선이 시그널선을 상향 돌파"}
    return None


def rule_macd_dead_cross(close, volume):
    """MACD선이 시그널선을 하향 돌파"""
    if len(close) < 35:
        return None
    macd_line, signal_line = _macd(close)
    signal = _cross_signal(macd_line, signal_line)
    if signal == -1:
        return {"label": "MACD 데드크로스", "detail": "MACD선이 시그널선을 하향 돌파"}
    return None


ALERT_RULES = [
    rule_volume_surge_at_lower_band,
    rule_volume_surge_at_upper_band,
    rule_golden_cross_short,
    rule_dead_cross_short,
    rule_golden_cross_mid,
    rule_dead_cross_mid,
    rule_rsi_overbought,
    rule_rsi_oversold,
    rule_macd_golden_cross,
    rule_macd_dead_cross,
]


def check_alerts(close, volume):
    alerts = []
    for rule in ALERT_RULES:
        try:
            result = rule(close, volume)
            if result:
                alerts.append(result)
        except Exception as e:
            print(f"[알림 규칙 오류] {rule.__name__}: {e}")
    return alerts


def get_domestic_price(ticker):
    from pykrx import stock

    today = datetime.now()
    fromdate = (today - timedelta(days=250)).strftime("%Y%m%d")
    todate = today.strftime("%Y%m%d")

    df = stock.get_market_ohlcv_by_date(fromdate, todate, ticker)
    df = df[df["종가"] > 0]
    if len(df) < 2:
        return None

    close, high, low, volume = df["종가"], df["고가"], df["저가"], df["거래량"]
    last_close = int(close.iloc[-1])
    prev_close = int(close.iloc[-2])
    pct = (last_close - prev_close) / prev_close * 100

    result = {"price": last_close, "pct": pct}

    result["ma5"] = round(close.rolling(5).mean().iloc[-1]) if len(close) >= 5 else None
    result["ma20"] = round(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
    result["ma60"] = round(close.rolling(60).mean().iloc[-1]) if len(close) >= 60 else None

    vol_avg20 = volume.rolling(20).mean().iloc[-1] if len(volume) >= 20 else None
    result["vol_ratio"] = round(volume.iloc[-1] / vol_avg20 * 100) if vol_avg20 else None

    if len(close) >= 20:
        mid, upper, lower = _bollinger(close)
        result["bb_upper"] = round(upper)
        result["bb_lower"] = round(lower)
        result["bb_pos"] = round((last_close - lower) / (upper - lower) * 100) if upper != lower else 50

    ichimoku = _ichimoku_cloud_position(high, low, close)
    if ichimoku:
        result["cloud_position"] = ichimoku["position"]

    zone = _heavy_volume_zone(df, "종가", "거래량")
    if zone:
        result["heavy_zone"] = f"{int(zone['low']):,}~{int(zone['high']):,}원"

    result["alerts"] = check_alerts(close, volume)

    try:
        trade_df = stock.get_market_trading_value_by_date(fromdate, todate, ticker)
        if "외국인합계" in trade_df.columns:
            result["foreign_5d"] = int(trade_df["외국인합계"].tail(5).sum())
        if "기관합계" in trade_df.columns:
            result["inst_5d"] = int(trade_df["기관합계"].tail(5).sum())
    except Exception as e:
        print(f"[외국인/기관 순매수 조회 오류] {ticker}: {e}")

    return result


def get_overseas_price(ticker):
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period="250d")
    if hist.empty or len(hist) < 2:
        return None

    close, high, low, volume = hist["Close"], hist["High"], hist["Low"], hist["Volume"]
    last_close = float(close.iloc[-1])
    prev_close = float(close.iloc[-2])
    pct = (last_close - prev_close) / prev_close * 100

    result = {"price": last_close, "pct": pct}

    result["ma5"] = round(close.rolling(5).mean().iloc[-1], 2) if len(close) >= 5 else None
    result["ma20"] = round(close.rolling(20).mean().iloc[-1], 2) if len(close) >= 20 else None
    result["ma60"] = round(close.rolling(60).mean().iloc[-1], 2) if len(close) >= 60 else None

    vol_avg20 = volume.rolling(20).mean().iloc[-1] if len(volume) >= 20 else None
    result["vol_ratio"] = round(volume.iloc[-1] / vol_avg20 * 100) if vol_avg20 else None

    if len(close) >= 20:
        mid, upper, lower = _bollinger(close)
        result["bb_upper"] = round(upper, 2)
        result["bb_lower"] = round(lower, 2)
        result["bb_pos"] = round((last_close - lower) / (upper - lower) * 100) if upper != lower else 50

    ichimoku = _ichimoku_cloud_position(high, low, close)
    if ichimoku:
        result["cloud_position"] = ichimoku["position"]

    zone = _heavy_volume_zone(hist, "Close", "Volume")
    if zone:
        result["heavy_zone"] = f"${zone['low']:.2f}~${zone['high']:.2f}"

    result["alerts"] = check_alerts(close, volume)

    # 외국인/기관 순매수는 해외 주식은 무료 공개 API로 구할 수 없어 생략
    return result


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
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    params = {"query": name, "display": display, "sort": "date"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        res.raise_for_status()
        items = res.json().get("items", [])
        return [
            {"title": clean_html(i["title"]), "url": i.get("link") or i.get("originallink") or "#"}
            for i in items
        ]
    except Exception as e:
        print(f"[뉴스 오류] {name}: {e}")
        return []


def translate_to_korean(text):
    """영문 뉴스 제목을 한글로 번역 (실패 시 원문 그대로 반환)"""
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="auto", target="ko").translate(text)
    except Exception as e:
        print(f"[번역 오류] {e}")
        return text


def get_overseas_news(ticker, count=2):
    import yfinance as yf

    try:
        news = yf.Ticker(ticker).news or []
        results = []
        for n in news[:count]:
            content = n.get("content", {})
            title = n.get("title") or content.get("title")
            url = (
                n.get("link")
                or content.get("canonicalUrl", {}).get("url")
                or content.get("clickThroughUrl", {}).get("url")
                or "#"
            )
            if title:
                results.append({"title": translate_to_korean(title), "url": url})
        return results
    except Exception as e:
        print(f"[뉴스 오류] {ticker}: {e}")
        return []


# ---------------------------------------------------------------------------
# 4. 카카오 토큰 갱신 / 전송
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
    total_alerts = 0
    for w in watchlist:
        if w["domestic"]:
            info = get_domestic_price(w["ticker"])
            news = get_domestic_news(w["name"])
            price_str = f"{info['price']:,}원" if info else "조회 실패"
            unit = "원"
        else:
            info = get_overseas_price(w["ticker"])
            news = get_overseas_news(w["ticker"])
            price_str = f"${info['price']:.2f}" if info else "조회 실패"
            unit = "$"
        pct = info["pct"] if info else 0
        color = "#d92626" if pct >= 0 else "#1a63d1"
        sign = "+" if pct >= 0 else ""
        news_html = "".join(
            f'<li><a href="{n["url"]}" target="_blank" rel="noopener">{n["title"]}</a></li>' for n in news
        ) or "<li>관련 뉴스 없음</li>"

        badges = []
        if info:
            def fmt(v):
                return f"{v:,.2f}" if unit == "$" else f"{v:,.0f}"

            if info.get("ma5") is not None:
                badges.append(f"MA5 {fmt(info['ma5'])}")
            if info.get("ma20") is not None:
                badges.append(f"MA20 {fmt(info['ma20'])}")
            if info.get("ma60") is not None:
                badges.append(f"MA60 {fmt(info['ma60'])}")
            if info.get("vol_ratio") is not None:
                badges.append(f"거래량 20일평균대비 {info['vol_ratio']}%")
            if info.get("bb_pos") is not None:
                badges.append(f"볼린저밴드 내 위치 {info['bb_pos']}%")
            if info.get("cloud_position"):
                badges.append(f"일목균형표 {info['cloud_position']}")
            if info.get("heavy_zone"):
                badges.append(f"매물대 {info['heavy_zone']}")
            if info.get("foreign_5d") is not None:
                v = info["foreign_5d"] / 1e8
                sign_f = "+" if v >= 0 else ""
                badges.append(f"외국인 5일 순매수 {sign_f}{v:,.1f}억원")
            if info.get("inst_5d") is not None:
                v = info["inst_5d"] / 1e8
                sign_i = "+" if v >= 0 else ""
                badges.append(f"기관 5일 순매수 {sign_i}{v:,.1f}억원")

        badges_html = "".join(f'<span class="badge">{b}</span>' for b in badges)

        alerts = info.get("alerts", []) if info else []
        total_alerts += len(alerts)
        alerts_html = "".join(
            f'<div class="alert"><b>🔔 {a["label"]}</b><span>{a["detail"]}</span></div>' for a in alerts
        )

        rows.append(f"""
        <div class="card">
          <div class="row">
            <span class="name">{w['name']}</span>
            <span class="price">{price_str} <span style="color:{color}">({sign}{pct:.2f}%)</span></span>
          </div>
          {alerts_html}
          <div class="badges">{badges_html}</div>
          <ul class="news">{news_html}</ul>
        </div>""")

    today = datetime.now().strftime("%Y-%m-%d")
    html = f"""<!DOCTYPE html>
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
  .badges {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }}
  .badge {{ background:#f0f0f3; color:#444; font-size:11px; padding:4px 8px; border-radius:20px; white-space:nowrap; }}
  .alert {{ background:#fff4e5; border:1px solid #ffcc80; border-radius:8px; padding:8px 10px; margin-bottom:10px; font-size:12px; color:#8a5300; display:flex; flex-direction:column; gap:2px; }}
  .news {{ margin:0; padding-left:18px; font-size:13px; color:#555; line-height:1.6; }}
  .news a {{ color:#555; text-decoration:none; }}
  .news a:hover {{ text-decoration:underline; }}
</style>
</head>
<body>
  <h1>📈 오늘의 관심 종목</h1>
  <div class="updated">{today} 업데이트 · 시세/지표/뉴스만 표시됩니다 (매매 조언 아님)</div>
  {''.join(rows)}
</body>
</html>"""
    return html, total_alerts


# ---------------------------------------------------------------------------
# 6. 메인
# ---------------------------------------------------------------------------
def main():
    portfolio = load_portfolio()
    watchlist = collect_watchlist(portfolio)

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    html, total_alerts = build_html(watchlist)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/")
    pages_url = f"https://{owner}.github.io/{repo}/"

    access_token = refresh_kakao_token()
    today = datetime.now().strftime("%Y-%m-%d")
    alert_note = f"\n🔔 관찰 알림 {total_alerts}건 발생" if total_alerts else ""
    text = f"📈 {today} 아침 브리핑이 준비됐어요.{alert_note}\n관심 종목 시세·뉴스 확인하기:\n{pages_url}"
    send_kakao_memo(access_token, text)

    print("완료")


if __name__ == "__main__":
    main()
