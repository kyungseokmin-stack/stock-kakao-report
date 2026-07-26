# 매일 아침 7시 포트폴리오 카톡 브리핑

가격(pykrx / yfinance) + 뉴스(네이버뉴스 검색 API / yfinance) 를 모아
매일 오전 7시(KST)에 카카오톡 "나에게 보내기"로 전송하는 자동화입니다.
GitHub Actions가 정해진 시간에 스크립트를 대신 실행해줍니다.

아래 단계는 전부 **한 번만** 하면 되고(카카오 재인증만 ~2개월마다 필요할 수 있음),
이후에는 완전 자동으로 돌아갑니다.

---

## 1. 카카오 개발자 앱 만들기 (필수)

1. https://developers.kakao.com 접속 → 로그인 → **내 애플리케이션 → 애플리케이션 추가**
2. 앱 이름 아무거나 입력 후 생성
3. **앱 설정 → 앱 키** 에서 **REST API 키** 복사 → 나중에 `KAKAO_REST_API_KEY`로 사용
4. **제품 설정 → 카카오 로그인** → 활성화 ON
5. **Redirect URI**에 아래 아무 URL이나 등록 (실제로 접속 안 해도 됨)
   ```
   https://localhost:3000
   ```
6. **카카오 로그인 → 동의항목** 에서 **카카오톡 메시지 전송 (talk_message)** 을 "필수 동의"로 설정

## 2. 인가 코드 → 토큰 발급 (최초 1회, 본인 브라우저에서)

1. 아래 URL의 `{REST_API_KEY}`를 본인 키로 바꿔서 브라우저 주소창에 붙여넣기
   ```
   https://kauth.kakao.com/oauth/authorize?client_id={REST_API_KEY}&redirect_uri=https://localhost:3000&response_type=code&scope=talk_message
   ```
2. 카카오 로그인 후 동의하면 `https://localhost:3000/?code=xxxxxxx` 로 리다이렉트됩니다.
   (페이지 로딩 실패해도 괜찮음, 주소창의 `code=` 뒤 값만 복사)
3. 터미널(또는 아무 파이썬 환경)에서 아래 실행해 토큰 발급:
   ```bash
   curl -X POST "https://kauth.kakao.com/oauth/token" \
     -d "grant_type=authorization_code" \
     -d "client_id={REST_API_KEY}" \
     -d "redirect_uri=https://localhost:3000" \
     -d "code={위에서 복사한 code}"
   ```
4. 응답 JSON에서 `refresh_token` 값을 복사 → `KAKAO_REFRESH_TOKEN`으로 사용
   (access_token은 몇 시간짜리라 저장할 필요 없음, 스크립트가 매번 자동 갱신함)

> ⚠️ 참고: 카카오 refresh_token은 약 2개월간 유효합니다. 아래 3단계에서
> `GH_PAT`을 설정해두면 만료 전 자동 갱신되지만, 설정 안 하면 2개월마다
> 이 2단계를 다시 해줘야 합니다.

## 3. 네이버 뉴스 API 키 발급 (국내 뉴스용)

1. https://developers.naver.com/apps/#/register 접속
2. 애플리케이션 등록 → 사용 API에서 **검색** 체크
3. 발급된 **Client ID / Client Secret** 복사

## 4. GitHub 저장소 만들고 파일 올리기

1. GitHub에서 새 저장소 생성 (Private 추천 – 토큰이 secrets로 들어가긴 하지만 코드 자체는 비공개가 안전)
2. 이 폴더(`stock-kakao-report`) 전체를 그 저장소에 업로드
3. 저장소 **Settings → Secrets and variables → Actions → New repository secret** 에서 아래 등록:
   | Secret 이름 | 값 |
   |---|---|
   | `KAKAO_REST_API_KEY` | 1단계에서 복사한 REST API 키 |
   | `KAKAO_REFRESH_TOKEN` | 2단계에서 복사한 refresh_token |
   | `NAVER_CLIENT_ID` | 3단계 Client ID |
   | `NAVER_CLIENT_SECRET` | 3단계 Client Secret |
   | `GH_PAT` (선택) | repo 권한 있는 Personal Access Token – refresh_token 자동 갱신용 |

`GH_PAT` 발급: GitHub 우측상단 프로필 → Settings → Developer settings →
Personal access tokens → Fine-grained tokens → 이 저장소만 대상으로
**Secrets: Read and write** 권한 부여.

## 5. 포트폴리오 채우기

`portfolio.json` 파일을 열어 실제 보유 종목으로 교체하세요.

- 국내 종목 코드는 네이버금융에서 종목 검색 시 URL의 6자리 숫자입니다.
- `quantity`/`avg_price`는 수익률 표시용이며, 없으면 그냥 `0`으로 둬도 됩니다.
- 토스/삼성증권 앱은 자동 연동이 불가능해서(공개 API 없음) 이 파일에
  직접 입력해주셔야 합니다. 종목이 바뀔 때마다 이 파일만 수정하면 됩니다.

## 6. 테스트 실행

저장소의 **Actions 탭 → Daily Stock Report to KakaoTalk → Run workflow**
버튼으로 수동 실행해서 카톡이 잘 오는지 먼저 확인하세요.
정상 동작하면 이후 매일 오전 7시(KST)에 자동으로 옵니다.

---

## 참고 / 한계

- pykrx, 네이버 API 모두 무료지만 호출량 정책이 있어 종목 수가 아주 많으면
  (수십 개 이상) 조절이 필요할 수 있습니다.
- yfinance의 뉴스 응답 구조는 버전에 따라 조금씩 달라질 수 있어
  코드에 방어 처리를 해뒀지만, 완전히 실패하면 가격 정보만 옵니다.
- 카카오 "나에게 보내기"는 본인에게만 보내는 API라 별도의 외부 발송 권한
  심사가 필요 없습니다.
