# KIS Auto Trading Template

한국투자증권 KIS Open API를 사용해 미국주식 자동매매 전략을 실험하기 위한 공개용 템플릿입니다.

이 저장소는 실제 운영 repo에서 민감정보와 개인 데이터를 제거한 버전입니다. 기본값은 `DRY_RUN=true`이며, 처음에는 반드시 모의투자와 가상주문으로만 테스트하세요.

## What It Does

- 한국투자증권 해외주식 API 토큰 준비
- 해외주식 잔고 조회
- 달러 예수금/매수가능금액 확인
- 나스닥100 universe 스캔
- QQQ 기준 시장 필터 확인
- 종목별 기술 지표 계산
- 매수 후보 점수화
- 매도 조건 판단
- 주문 판단 결과를 CSV/JSON으로 저장
- 선택적으로 별도 portfolio-data repo에 JSON push

## Strategy Summary

이 전략은 하락한 종목을 싸게 줍는 방식이 아닙니다.

```text
나스닥100 안에서
추세가 살아 있고
최근 급락/갭하락/과열/거래량 이상이 없고
QQQ 대비 상대적으로 강하며
계좌 규모 기준으로 살 수 있는 종목을
하루 1~2개 이하로 분산 매수하는 성장형 스윙 전략
```

## Investment Universe

- 국내주식은 거래하지 않습니다.
- 해외주식 중 미국주식만 거래합니다.
- 기본 universe는 나스닥100입니다.
- ETF `QQQ`는 시장 필터와 fallback 후보로 사용합니다.
- S&P500은 향후 보조 universe로 확장할 수 있게 구조화되어 있습니다.

## Market Filter

시장 상태는 `QQQ` 기준입니다.

```text
strong:
QQQ 현재가 > QQQ 20일 이동평균 > QQQ 60일 이동평균

normal:
QQQ 현재가 > QQQ 20일 이동평균

weak:
QQQ 현재가 <= QQQ 20일 이동평균
```

시장 상태별 신규 매수 한도:

```text
strong: 하루 최대 2종목
normal: 하루 최대 1종목
weak: 기본 0종목
```

초기 성장 모드에서는 weak 시장이어도 QQQ 대비 상대강도와 점수가 충분히 좋은 종목은 하루 최대 1종목까지 매수할 수 있습니다.

## Buy Candidate Filters

나스닥100 종목을 순회하면서 아래 조건을 통과한 종목만 매수 후보가 됩니다.

```text
1. 현재 보유 중인 종목이 아님
2. 수동 제외 종목이 아님
3. 최근 60거래일 데이터가 있음
4. 현재가 > 20일 이동평균
5. 5일선 > 20일선 > 60일선
6. 최근 5거래일 상승률 <= 15%
7. 최근 20거래일 상승률 <= 25%
8. 당일 등락률이 -1% ~ +4% 사이
9. 갭하락이 -2.5%보다 크지 않음
10. 절대 갭 변동폭이 4% 이하
11. 현재가가 60일선 대비 +25% 이내
12. 52주 고점의 98% 이상이면 제외
13. 20일 변동성 <= 6.5%
14. 당일 고저 변동폭 <= 6%
15. 전일 수익률 >= -3%
16. 최근 3일 수익률 >= -4%
17. 하락 중 거래량 급증이 2.5배 이상이면 제외
18. 20일 평균 거래대금 >= 10억 달러
```

## Scoring

필터를 통과한 종목은 아래 기준으로 점수화됩니다.

```text
현재가 > 20일선
→ +20점

5일선 > 20일선 > 60일선
→ +30점

최근 5일 상승률
<= 5%  → +15점
<= 10% → +10점
<= 15% → +5점

60일선 대비 이격도
<= 10% → +15점
<= 20% → +10점
<= 25% → +5점

20일 평균 거래대금 10억 달러 이상
→ +10점

20일 변동성 5% 이하
→ +10점

QQQ 대비 상대강도 양호
→ +10점

QQQ 대비 상대강도 부족
→ -10점

여행/항공/숙박 경기민감 리스크
→ -10점
```

기본 경기민감 리스크 대상은 `BKNG`, `ABNB`, `MAR`입니다. 이 항목은 매수 차단이 아니라 점수 감점입니다.

## Position Sizing

일반 계좌 기준:

```text
최대 보유 종목 수: 10
종목당 목표 비중: 총자산의 9.5%
종목당 최대 비중: 총자산의 12%
현금 버퍼: 5%
최소 주문금액: 50달러
```

소액 계좌 모드:

```text
총자산 2,000달러 이하
최대 보유 종목 수: 3
종목당 목표 비중: 총자산의 30%
종목당 최대 비중: 총자산의 45%
첫 1주 가격이 사용 가능 현금의 90%를 넘으면 매수 금지
```

## Sell Rules

보유 종목은 신규매수 가능 여부와 관계없이 매도 판단을 계속 실행합니다.

```text
+20% 이상 수익: 전량 매도
+10% 이상 수익: 절반 매도, 반복 절반매도 방지
+5% 이상 수익 + 현재가 < 20일선: 전량 매도
-8% 이하 손실 + 20일선/60일선 약세 + 최근 5일 음수: 전량 손절
매수 직후 3거래일 이내 -4.5% 이하 + 20일선 아래 + 최근 3일 음수: 조기 방어 매도
15거래일 이상 보유 + 수익률 5% 미만 + 흐름 약화: 전량 매도
```

## Rebuy Cooldown

같은 날 매도한 종목은 같은 날 다시 사지 않습니다.

```text
+10% 절반익절: 1일
+20% 전량익절: 3일
20일선 이탈 익절: 2일
손절: 5일
횡보/부진 매도: 2일
조기 방어 매도: 3일
```

## Quick Start

1. 저장소를 fork하거나 template으로 생성합니다.
2. `.env.example`을 참고해 `.env`를 만듭니다.
3. 한국투자증권 KIS Open API 키를 입력합니다.
4. 처음에는 모의투자 URL과 `DRY_RUN=true`를 사용합니다.
5. 의존성을 설치합니다.

```bash
pip install -r requirements.txt
```

6. 단위 테스트를 실행합니다.

```bash
python -m unittest discover -s tests
```

7. 진단 모드를 실행합니다.

```bash
python auto_trader.py --mode diagnose
```

8. 점수만 확인합니다.

```bash
python auto_trader.py --mode score-only
```

9. 가상 매매로 전체 흐름을 확인합니다.

```bash
python auto_trader.py --mode full
```

## Run Modes

```text
diagnose             계좌/시장 진단
account-diagnose     계좌 응답 상세 진단
token-test           토큰 준비 테스트
portfolio-auth-test  portfolio-data repo push 권한 테스트
strategy-test        GitHub Actions용 DRY_RUN 전략 테스트
unit-test            단위 테스트
paper-order-test     모의투자 주문 테스트
sell-only            보유 종목 매도 판단만 실행
cancel-open-orders   미체결 해외주문 전체 취소
portfolio-snapshot   포트폴리오 JSON만 갱신
score-only           점수 계산만 실행
full                 매도 판단 + 매수 판단 전체 실행
```

## GitHub Actions

`.github/workflows/auto-trader.yml`은 수동 실행과 외부 dispatch 실행을 지원합니다.

필요한 Secrets:

```text
KIS_APP_KEY
KIS_APP_SECRET
KIS_CANO
KIS_ACNT_PRDT_CD
KIS_BASE_URL
DRY_RUN
PORTFOLIO_DATA_REPO_URL
```

`PORTFOLIO_DATA_REPO_URL` 예시:

```text
https://x-access-token:YOUR_GITHUB_TOKEN@github.com/YOUR_GITHUB_ID/kis-portfolio-data.git
```

처음에는 반드시 아래 값으로 시작하는 것을 권장합니다.

```text
DRY_RUN=true
KIS_BASE_URL=https://openapivts.koreainvestment.com:29443
```

## Google Apps Script Example

`google_apps_script_scheduler.example.js`는 GitHub Actions workflow를 외부에서 호출하는 예시입니다.

파일 안의 값을 본인 repo에 맞게 수정하세요.

```javascript
const GITHUB_OWNER = 'YOUR_GITHUB_ID';
const GITHUB_REPO = 'kis-auto-trading-template';
```

Apps Script의 Script Properties에는 `GITHUB_TOKEN`을 저장해야 합니다.

## Safety Notes

- 이 repo에는 실제 API 키, 토큰, 계좌번호, 주문 로그를 커밋하지 마세요.
- 운영 전에는 모의투자와 `DRY_RUN=true`로 충분히 검증하세요.
- 실매매를 켜려면 `DRY_RUN=false`가 필요하지만, 그 전에는 반드시 전략과 주문 로직을 직접 이해해야 합니다.
- GitHub Actions 로그에 민감정보가 출력되지 않는지 확인하세요.

## Disclaimer

이 저장소는 자동매매 시스템을 학습하고 실험하기 위한 템플릿입니다. 투자 권유가 아니며, 실제 투자 판단과 손익 책임은 사용자 본인에게 있습니다.
