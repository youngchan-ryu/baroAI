"""Fictional cases for a separately owned booth generation.

Order matters: end finished visits before starting the active visits.
"""

from typing import NamedTuple


class ShowcaseReport(NamedTuple):
    result: str
    text: str
    actions: tuple[str, ...]
    materials: tuple[str, ...] = ()
    residual_issue: str = ""


SHOWCASE_REPORTS = {
    "showcase-done-a": ShowcaseReport(
        "completed",
        "301동 강의실 입구 스위치 본체를 교체했습니다. "
        "스위치를 세 차례 눌러 조명의 점등과 소등을 확인했습니다.",
        ("강의실 입구 스위치 본체 교체", "세 차례 점등·소등 확인"),
        ("스위치 1개",),
    ),
    "showcase-done-b": ShowcaseReport(
        "completed",
        "302동 2층 세면대 배수 연결부의 패킹을 교체했습니다. "
        "물을 흘려보낸 뒤 연결부에 물방울이 맺히지 않는 것을 확인했습니다.",
        ("세면대 배수 연결부 패킹 교체", "통수 후 연결부 누수 여부 확인"),
        ("배수 연결부 패킹 1개",),
    ),
    "showcase-temporary": ShowcaseReport(
        "temporary",
        "행정관 출입문 손잡이 고정 나사를 조여 흔들림을 줄였습니다. "
        "손잡이 축이 닳아 손잡이 세트를 교체해야 합니다.",
        ("출입문 손잡이 고정 나사 조임",),
        residual_issue="손잡이 축 마모로 손잡이 세트 교체가 필요합니다.",
    ),
    "showcase-report-a": ShowcaseReport(
        "revisit",
        "배수구 덮개 주변에 안전표지를 세우고 들뜬 덮개의 받침을 확인했습니다. "
        "받침 가장자리가 깨져 덮개 받침을 교체할 때 다시 방문해야 합니다.",
        ("배수구 덮개 주변 안전표지 설치", "들뜬 덮개와 받침 상태 확인"),
        residual_issue="파손된 덮개 받침 교체가 필요합니다.",
    ),
    "showcase-report-b": ShowcaseReport(
        "completed",
        "관정관 복도에서 깜빡이던 조명의 LED 구동기를 교체했습니다. "
        "전원을 다시 켜고 10분간 점등 상태를 확인했습니다.",
        ("조명 LED 구동기 교체", "10분간 점등 상태 확인"),
        ("LED 구동기 1개",),
    ),
    "showcase-confirm-b": ShowcaseReport(
        "completed",
        "행정관 회의실 냉방기의 송풍 팬 구동 콘덴서를 교체했습니다. "
        "냉방을 다시 켜고 10분 동안 송풍과 냉기 공급이 이어지는 것을 확인했습니다.",
        ("송풍 팬 구동 콘덴서 교체", "10분간 송풍과 냉기 공급 확인"),
        ("송풍 팬 구동 콘덴서 1개",),
    ),
}

SHOWCASE_FINAL_REPORT = ShowcaseReport(
    "completed",
    "행정관 출입문 손잡이 세트를 교체했습니다. "
    "문을 다섯 차례 여닫아 손잡이 흔들림과 걸림이 없는 것을 확인했습니다.",
    ("출입문 손잡이 세트 교체", "다섯 차례 개폐로 흔들림·걸림 확인"),
    ("손잡이 세트 1개",),
)

# The ticket stays in review once this exchange has closed.
SHOWCASE_ANSWERED_REQUEST = (
    "showcase-priority-a",
    "물이 고인 구간이 어느 층 어느 쪽인가요?",
    "2층 동쪽 계단에서 복도로 나오는 모퉁이입니다.",
)

# A person can act on these concrete observations without changing the original AI inputs.
SHOWCASE_URGENCY_OVERRIDES = {
    "showcase-confirm-a": (
        3, "세면대 연결부에만 물방울이 맺혀 있고 바닥으로 떨어지는 물은 없습니다."
    ),
    "showcase-queued-a": (2, "계단참의 조명이 꺼져 발 디딜 곳이 잘 보이지 않아 먼저 점검합니다."),
}

# name, location, code, text, category, target, worker, urgency, age in hours
SHOWCASE = (
    (
        "showcase-done-a",
        "관악 301동",
        "GWANAK_301",
        "301동 강의실 입구 스위치를 눌러도 조명이 켜지지 않습니다.",
        "ELEC_OUTLET_SWITCH",
        "completed",
        "worker_a",
        2,
        145,
    ),
    (
        "showcase-done-b",
        "관악 302동",
        "GWANAK_302",
        "302동 2층 화장실 세면대 아래 연결부에서 물이 조금씩 떨어집니다.",
        "MECH_WATER_LEAK",
        "completed",
        "worker_b",
        2,
        97,
    ),
    (
        "showcase-temporary",
        "관악 60동",
        "GWANAK_60",
        "행정관 복도 출입문 손잡이가 헐거워 문을 열 때 흔들립니다.",
        "ARCH_DOOR_WINDOW_LOCK",
        "temporary",
        "worker_c",
        2,
        20,
    ),
    (
        "showcase-report-a",
        "관악 300동",
        "GWANAK_300",
        "300동 앞 보행로의 배수구 덮개가 밟을 때 흔들립니다.",
        "CIVIL_DRAINAGE",
        "report",
        "worker_d",
        2,
        121,
    ),
    (
        "showcase-report-b",
        "관악 62-1동",
        "GWANAK_62-1",
        "관정관 복도 천장 조명 한 개가 계속 깜빡입니다.",
        "ELEC_LIGHTING",
        "completed",
        "worker_a",
        2,
        73,
    ),
    (
        "showcase-progress-a",
        "관악 301동",
        "GWANAK_301",
        "301동 화장실 바닥에 물이 고이고 세면대 아래에서 물이 떨어집니다.",
        "MECH_WATER_LEAK",
        "progress",
        "worker_b",
        2,
        12,
    ),
    (
        "showcase-progress-b",
        "관악 60동",
        "GWANAK_60",
        "행정관 출입구 문이 바닥에 닿아 끝까지 열리지 않습니다.",
        "ARCH_DOOR_WINDOW_LOCK",
        "progress",
        "worker_c",
        2,
        14,
    ),
    (
        "showcase-queued-a",
        "관악 302동",
        "GWANAK_302",
        "302동 계단참 조명이 켜지지 않아 통행할 때 어둡습니다.",
        "ELEC_LIGHTING",
        "queued",
        "worker_a",
        2,
        16,
    ),
    (
        "showcase-queued-b",
        "관악 25동",
        "GWANAK_25",
        "25동 복도 천장 조명이 켜지지 않아 점검을 요청합니다.",
        "ELEC_LIGHTING",
        "queued",
        "worker_a",
        2,
        18,
    ),
    (
        "showcase-confirm-a",
        "관악 62-1동",
        "GWANAK_62-1",
        "관정관 화장실 세면대 배관 연결부에 물방울이 맺힙니다.",
        "MECH_WATER_LEAK",
        "confirmed",
        None,
        1,
        1.5,
    ),
    (
        "showcase-confirm-b",
        "관악 60동",
        "GWANAK_60",
        "행정관 회의실 냉방기를 켜도 바람이 나오지 않습니다.",
        "MECH_HVAC",
        "completed",
        "worker_f",
        3,
        11,
    ),
    (
        "showcase-info",
        "관악 301동",
        "GWANAK_301",
        "301동 복도 벽 아래쪽에 젖은 듯한 얼룩이 생겼습니다.",
        "ARCH_WATERPROOF",
        "information",
        None,
        3,
        13,
    ),
    (
        "showcase-urgent",
        "관악 300동",
        "GWANAK_300",
        "300동 분전함에서 연기가 보여 주변에서 떨어져 신고합니다.",
        "ELEC_POWER_OUTAGE",
        "review",
        None,
        0,
        0.2,
    ),
    (
        "showcase-priority-a",
        "관악 301동",
        "GWANAK_301",
        "301동 복도 바닥에 물이 고여 지나갈 때 미끄럽습니다.",
        "MECH_WATER_LEAK",
        "review",
        None,
        1,
        0.5,
    ),
    (
        "showcase-priority-b",
        "관악 302동",
        "GWANAK_302",
        "302동 출입문이 끝까지 닫히지 않아 확인이 필요합니다.",
        "ARCH_DOOR_WINDOW_LOCK",
        "review",
        None,
        1,
        0.75,
    ),
)
