"""예상 비용 계산 (LLM이 숫자를 만들지 않게 코드로 계산).

단가는 prices.json 에서 읽는다. 단가마다 출처(source)와 확인일(checked)이 있어야 하고,
값이 비어 있으면(null) 금액 대신 null 과 "단가 확인 전" 메모를 돌려준다. 추측 금액은 내지 않는다.
"""
import json
from pathlib import Path

_PRICES = json.loads((Path(__file__).with_name("prices.json")).read_text(encoding="utf-8"))
# 같은 인스턴스 단가를 쓰는 대상 (ec2_compose = 같은 EC2 인스턴스 타입에 컨테이너만 여러 개)
SAME_PRICE_AS = {"aws_ec2_compose": "aws_ec2"}

# 비교용 사용량 가정 (화면에 그대로 보여준다)
ASSUMPTIONS = {
    "test_hours": 1,                 # Pawploy 테스트 배포는 1시간 뒤 삭제
    "monthly_requests": 100_000,     # 월 10만 요청
    "avg_request_seconds": 0.2,      # 요청당 평균 0.2초
}


def estimate(target_id: str, size: str) -> dict:
    p = (_PRICES.get(SAME_PRICE_AS.get(target_id, target_id)) or {}).get(size)
    base = {"assumptions": ASSUMPTIONS, "currency": "USD"}
    if not p or p.get("hourly") is None and p.get("per_request_second") is None:
        return {**base, "test_1h": None, "monthly": None, "note": "단가 확인 전"}
    a = ASSUMPTIONS
    if p.get("hourly") is not None:                    # 켜져 있는 시간만큼 과금 (EC2 등)
        test = p["hourly"] * a["test_hours"]
        monthly = p["hourly"] * 730 + (p.get("monthly_fixed") or 0)
    else:                                              # 요청 시간만큼 과금 (Lambda, Cloud Run)
        busy = a["monthly_requests"] * a["avg_request_seconds"]
        monthly = busy * p["per_request_second"] + a["monthly_requests"] * (p.get("per_request") or 0)
        test = 0.0
    return {**base, "test_1h": round(test, 4), "monthly": round(monthly, 2),
            "source": p.get("source"), "checked": p.get("checked")}
