"""Internal review suggestions, never delivery confirmation."""

from app.catalog import CATEGORY_MAP
from app.services.location import BY_CODE

VERSION = "routing-synthetic-v1"
LABELS = {
    "REVIEW": "담당자 검토",
    "FACILITY_REVIEW": "시설 분야 검토",
    "FAC_ARCH_CIVIL": "건축 분야 검토",
    "FAC_CIVIL": "토목 분야 검토",
    "FAC_ELEC": "전기 분야 검토",
    "FAC_MECH": "기계 분야 검토",
    "IT": "정보통신 분야 검토",
    "LAB_SAFETY": "실험실 안전 분야 검토",
    "DORM": "생활관 분야 검토",
    "CAMPUS_SAFETY": "안전 분야 검토",
    "CONSTRUCTION": "공사 소관 검토",
    "CLEANING": "청소 분야 검토",
    "LANDSCAPE": "조경 분야 검토",
}


def suggest(category_code, location, safety, catalog_snapshot=None):
    if catalog_snapshot and catalog_snapshot.get("catalog_backed"):
        department = catalog_snapshot.get("department_code") or "REVIEW"
        return {
            "department_code": department,
            "department_label": catalog_snapshot.get("department_name") or "담당자 확인 필요",
            "co_review": [],
            "status": "suggested",
            "delivery_status": "not_sent",
            "rules_version": VERSION,
            "reason": "학교 설정에 따른 내부 검토 제안 · 사람 확인 필요",
        }
    department = CATEGORY_MAP[category_code]["department_code"]
    selected = location.get("selected")
    reason = "분류에 따른 내부 검토 제안 · 실제 업무 분장 미확인"
    if (
        location.get("state") == "confirmed"
        and selected
        and BY_CODE.get(selected["building_code"], {}).get("dorm")
    ):
        department, reason = "DORM", "사람이 확인한 합성 생활관 · 생활관 분야 검토 제안"
    co_review = set()
    rules = {h["rule_id"] for h in safety.get("rule_hits", [])}
    if rules & {"fire", "gas", "collapse", "injury", "construction", "elevator_stop"}:
        co_review.add("CAMPUS_SAFETY")
    if rules & {"electric", "water_electric"}:
        co_review.add("FAC_ELEC")
    if category_code == "XFER_CONSTRUCTION_SITE":
        co_review.add("CAMPUS_SAFETY")
    return {
        "department_code": department,
        "department_label": LABELS[department],
        "co_review": [{"code": code, "label": LABELS[code]} for code in sorted(co_review)],
        "status": "suggested",
        "delivery_status": "not_sent",
        "rules_version": VERSION,
        "reason": reason,
    }
