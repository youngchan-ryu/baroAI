LEGACY_CATEGORIES = [
    {
        "code": "UNKNOWN",
        "label": "담당자 확인 필요",
        "domain": "UNKNOWN",
        "department_code": "REVIEW",
    },
    {
        "code": "PLUMBING",
        "label": "급배수",
        "domain": "FACILITY",
        "department_code": "FACILITY_REVIEW",
    },
    {
        "code": "ELECTRICAL",
        "label": "전기",
        "domain": "FACILITY",
        "department_code": "FACILITY_REVIEW",
    },
    {"code": "HVAC", "label": "냉난방", "domain": "FACILITY", "department_code": "FACILITY_REVIEW"},
    {
        "code": "BUILDING",
        "label": "건축·시설",
        "domain": "FACILITY",
        "department_code": "FACILITY_REVIEW",
    },
]
CATEGORIES = [
    {
        "code": "ARCH_DOOR_WINDOW_LOCK",
        "label": "문·창·잠금",
        "domain": "ARCH_CIVIL",
        "department_code": "FAC_ARCH_CIVIL",
    },
    {
        "code": "ARCH_FLOOR_WALL_CEILING",
        "label": "바닥·벽·천장",
        "domain": "ARCH_CIVIL",
        "department_code": "FAC_ARCH_CIVIL",
    },
    {
        "code": "ARCH_GLASS",
        "label": "유리",
        "domain": "ARCH_CIVIL",
        "department_code": "FAC_ARCH_CIVIL",
    },
    {
        "code": "ARCH_WATERPROOF",
        "label": "방수",
        "domain": "ARCH_CIVIL",
        "department_code": "FAC_ARCH_CIVIL",
    },
    {
        "code": "ARCH_ACCESSIBILITY",
        "label": "장애인 편의시설",
        "domain": "ARCH_CIVIL",
        "department_code": "FAC_ARCH_CIVIL",
    },
    {
        "code": "ARCH_FIRE_DOOR",
        "label": "방화문·건축 소방",
        "domain": "ARCH_CIVIL",
        "department_code": "FAC_ARCH_CIVIL",
    },
    {
        "code": "ARCH_OTHER",
        "label": "건축 기타",
        "domain": "ARCH_CIVIL",
        "department_code": "FAC_ARCH_CIVIL",
    },
    {
        "code": "CIVIL_ROAD_SIDEWALK",
        "label": "도로·보도",
        "domain": "CIVIL",
        "department_code": "FAC_CIVIL",
    },
    {
        "code": "CIVIL_DRAINAGE",
        "label": "배수로·맨홀",
        "domain": "CIVIL",
        "department_code": "FAC_CIVIL",
    },
    {
        "code": "CIVIL_RETAINING_WALL",
        "label": "옹벽·석축",
        "domain": "CIVIL",
        "department_code": "FAC_CIVIL",
    },
    {
        "code": "CIVIL_OTHER",
        "label": "토목 기타",
        "domain": "CIVIL",
        "department_code": "FAC_CIVIL",
    },
    {"code": "ELEC_LIGHTING", "label": "조명", "domain": "ELEC", "department_code": "FAC_ELEC"},
    {
        "code": "ELEC_OUTLET_SWITCH",
        "label": "콘센트·스위치",
        "domain": "ELEC",
        "department_code": "FAC_ELEC",
    },
    {"code": "ELEC_POWER_OUTAGE", "label": "정전", "domain": "ELEC", "department_code": "FAC_ELEC"},
    {
        "code": "ELEC_TELECOM_PHONE",
        "label": "통신·구내전화",
        "domain": "ELEC",
        "department_code": "FAC_ELEC",
    },
    {
        "code": "ELEC_FIRE_ALARM",
        "label": "전기 소방(감지기·경보)",
        "domain": "ELEC",
        "department_code": "FAC_ELEC",
    },
    {"code": "ELEC_ELEVATOR", "label": "승강기", "domain": "ELEC", "department_code": "FAC_ELEC"},
    {
        "code": "ELEC_EV_CHARGER",
        "label": "전기차 충전",
        "domain": "ELEC",
        "department_code": "FAC_ELEC",
    },
    {"code": "ELEC_OTHER", "label": "전기 기타", "domain": "ELEC", "department_code": "FAC_ELEC"},
    {"code": "MECH_HVAC", "label": "냉난방·공조", "domain": "MECH", "department_code": "FAC_MECH"},
    {"code": "MECH_WATER_LEAK", "label": "누수", "domain": "MECH", "department_code": "FAC_MECH"},
    {
        "code": "MECH_PLUMBING_CLOG",
        "label": "배관·변기·세면대 막힘",
        "domain": "MECH",
        "department_code": "FAC_MECH",
    },
    {
        "code": "MECH_SANITARY_FIXTURE",
        "label": "위생기구 파손(수전·변기 부품)",
        "domain": "MECH",
        "department_code": "FAC_MECH",
    },
    {"code": "MECH_HOT_WATER", "label": "급탕", "domain": "MECH", "department_code": "FAC_MECH"},
    {
        "code": "MECH_FIRE_SUPPRESSION",
        "label": "기계 소방(스프링클러·소화전)",
        "domain": "MECH",
        "department_code": "FAC_MECH",
    },
    {"code": "MECH_OTHER", "label": "기계 기타", "domain": "MECH", "department_code": "FAC_MECH"},
    {"code": "XFER_IT", "label": "네트워크·IT", "domain": "TRANSFER", "department_code": "IT"},
    {
        "code": "XFER_LAB_SAFETY",
        "label": "실험실·화학",
        "domain": "TRANSFER",
        "department_code": "LAB_SAFETY",
    },
    {"code": "XFER_DORM", "label": "기숙사", "domain": "TRANSFER", "department_code": "DORM"},
    {
        "code": "XFER_CAMPUS_SAFETY",
        "label": "보안·안전",
        "domain": "TRANSFER",
        "department_code": "CAMPUS_SAFETY",
    },
    {
        "code": "XFER_CONSTRUCTION_SITE",
        "label": "공사 현장 위험",
        "domain": "TRANSFER",
        "department_code": "CONSTRUCTION",
    },
    {"code": "XFER_CLEANING", "label": "청소", "domain": "TRANSFER", "department_code": "CLEANING"},
    {
        "code": "XFER_LANDSCAPE",
        "label": "조경·수목",
        "domain": "TRANSFER",
        "department_code": "LANDSCAPE",
    },
    {
        "code": "UNKNOWN",
        "label": "미분류 · 담당자 확인",
        "domain": "UNKNOWN",
        "department_code": "REVIEW",
    },
] + [c for c in LEGACY_CATEGORIES if c["code"] != "UNKNOWN"]
CATEGORY_MAP = {item["code"]: item for item in CATEGORIES}
EVENT_LABELS = {
    "report_dismissed": "과거 배정 보고 검토 종료 · 승인 아님",
    "merged": "담당자 승인으로 함께 처리",
    "merge_received": "연결 신고 추가 · 안전 정보 함께 확인",
    "merge_cancelled": "병합 취소 · 독립 처리 복구",
    "merge_source_restored": "연결 신고 복구 · 안전 재확인 필요",
    "merge_subscription_updated": "처리 소식 구독 변경",
    "merge_safety_reviewed": "연결 안전 정보 재확인",
    "duplicate_dismissed": "중복 후보 제외",
    "report_analysis_failed": "보고 재생 분석 실패 · 수동 확인 필요",
    "report_confirmed": "작업자 보고 확인 · 담당자 승인 대기",
    "report_correction_requested": "담당자 보고 보완 요청",
    "report_analysis_succeeded": "재생 보고 초안 · 작업자 확인 필요",
    "analysis_matched_replay": "고정 합성 예시 재생 · 사람 확인 필요",
    "analysis_unmatched": "재생 예시 불일치 · AI 판단 미정 · 사람 확인 필요",
    "analysis_succeeded": "과거 분석 처리 기록 · 예시 일치 여부 미확인",
    "analysis_failed": "재생 분석 실패 · 사람 확인 필요",
    "location_updated": "위치 확인 갱신",
    "urgency_overridden": "담당자 긴급도 수정",
    "created": "신고 작성",
    "received": "접수 · 사람 확인 대기",
    "edited": "담당자 수정",
    "confirmed": "담당자 확인",
    "assigned": "담당 작업 배정",
    "reassigned": "담당 작업 재배정",
    "started": "작업 착수",
    "reported": "작업 보고 · 승인 대기",
    "report_reviewed": "담당자 보고 확인",
}


EVENT_LABELS.update(
    {
        "departed": "이동 시작 확인",
        "visit_ended": "이번 방문 종료 확인",
        "visit_stopped": "업무 중지 확인 · 해결 완료 아님",
        "remaining_confirmed": "담당자가 잔여시간 확인",
        "urgent_plan_assigned": "긴급 삽입 계획 승인 후 배정",
        "urgent_plan_unassigned": "미배정 업무 담당자 재검토 필요",
        "plan_cancelled": "계획 취소 후 재배정 필요",
        "dispatch_plan_cancelled": "계획 전체 취소 확인",
    }
)


def school_location_candidates(content, text, location_text):
    """Match only catalog names and newline-separated aliases, without fuzzy selection."""
    import json
    import re
    import unicodedata

    def normalize(value):
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

    inputs = [normalize(text), normalize(location_text)]
    # A present identity is authoritative. Only legacy summaries without an
    # identity may use the digest of the previously reviewed dataset.
    dataset = content.get("dataset") or {}
    is_snu = False
    if isinstance(dataset, dict):
        if "canonical" in dataset:
            try:
                canonical = json.loads(dataset["canonical"])
                identity = canonical.get("dataset_id") if isinstance(canonical, dict) else None
            except (TypeError, ValueError):
                identity = None
            if "dataset_id" in dataset and dataset["dataset_id"] != identity:
                identity = None
        elif "dataset_id" in dataset:
            identity = dataset["dataset_id"]
        else:
            identity = None
            reviewed_dataset_digest = (
                "de34c6776e912023f4c495a61968b58e"
                "f1dd2da0d4f67ead226d66395f365283"
            )
            is_snu = dataset.get("sha256") == reviewed_dataset_digest
        if isinstance(identity, str):
            is_snu = bool(re.fullmatch(r"SNU_PUBLIC_[0-9]{8}(?:_R[0-9]{2})?", identity))
    campus_names = {
        "GWANAK": "관악", "YEONGEON": "연건", "PYEONGCHANG": "평창",
        "SIHEUNG": "시흥", "SUWON": "수원",
    }
    qualifiers = {**{name: code for code, name in campus_names.items()},
                  **{code.casefold(): code for code in campus_names}}
    prefix = re.compile(
        r"(?<![0-9a-z가-힣_])(?:(관악|연건|평창|시흥|수원)(?:\s*캠퍼스)?\s*"
        r"|(gwanak|yeongeon|pyeongchang|siheung|suwon)(?:\s*캠퍼스)?\s+)$"
    )

    def snu_building_match(location):
        if not is_snu:
            return None
        code = location.get("code", "")
        match = re.fullmatch(r"(GWANAK|YEONGEON|PYEONGCHANG|SIHEUNG|SUWON)_(\d+(?:-\d+)?)", code)
        if (not match or location.get("building_code") != code
                or location.get("region") != match.group(1)
                or not normalize(location.get("name", "")).startswith(
                    f"{campus_names[match.group(1)]} {match.group(2)}동 "
                )
                or location.get("space_code") != "BUILDING"
                or f"{match.group(2)}동" not in (location.get("aliases") or "").splitlines()):
            return None
        return match

    def scoped_number_allowed(match):
        if not match:
            return True
        campus, number = match.groups()
        number_pattern = re.compile(r"(?<![\d-])" + re.escape(number) + r"동(?!\d)")
        occurrences_by_input = []
        for value in inputs:
            occurrences = set()
            for occurrence in number_pattern.finditer(value):
                qualifier = prefix.search(value[:occurrence.start()])
                occurrences.add(
                    qualifiers[qualifier.group(1) or qualifier.group(2)] if qualifier else None
                )
            occurrences_by_input.append(occurrences)
        description_matches, entered_matches = occurrences_by_input
        if len(entered_matches) == 1 and None not in entered_matches:
            if not (description_matches - entered_matches - {None}):
                return campus in entered_matches
        occurrences = description_matches | entered_matches
        return not occurrences or None in occurrences or campus in occurrences

    candidates = []
    for location in content.get("locations", []):
        aliases = location.get("aliases") or ""
        terms = [normalize(term) for term in [location.get("name", ""), *aliases.splitlines()]]

        def matches(term, value):
            # Numeric building identifiers must not match a suffix of a longer number.
            chunks, offset = [], 0
            for match in re.finditer(r'\d+(?:-\d+)?동', term):
                chunks.extend((re.escape(term[offset:match.start()]),
                               rf'(?<![\d-]){re.escape(match.group())}(?!\d)'))
                offset = match.end()
            pattern = ''.join(chunks) + re.escape(term[offset:])
            return bool(term and re.search(pattern, value))

        matching = [term for term in terms if any(matches(term, value) for value in inputs)]
        numeric = any(re.fullmatch(r'\d+(?:-\d+)?동', term) for term in matching)
        building = snu_building_match(location)
        if building and location.get("floor") == "미확인":
            name_prefix = f"{campus_names[building.group(1)]} {building.group(2)}동 "
            bare_name = normalize(location.get("name", ""))[len(name_prefix):]
            if (bare_name and bare_name != "미확인" and "명칭 미확인" not in bare_name
                    and inputs[1] == bare_name):
                matching.append(bare_name)
        if matching and scoped_number_allowed(building):
            candidates.append((not numeric, {
                "code": location["code"],
                "label": location["name"],
                "building_code": location["building_code"],
                "floor": (
                    None
                    if location["floor"] == "미확인" and location["space_code"] == "BUILDING"
                    else str(location["floor"])
                ),
                "space_code": location["space_code"],
            }))
    candidates.sort(key=lambda item: item[0])
    return [item for _, item in candidates[:5]]
