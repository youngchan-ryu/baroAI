"""Synthetic booth inputs, with separate live and isolated-history boundaries."""

from app.booth_showcase import SHOWCASE, SHOWCASE_FINAL_REPORT, SHOWCASE_REPORTS

# The location codes are from the R08 candidate catalog. Descriptions are fictional.
SCENARIOS = (
    (
        "danger",
        "관악 301동",
        "GWANAK_301",
        "분전함에서 타는 냄새와 연기가 나요. 가까이 가지 않고 확인을 요청합니다.",
        "ELEC_POWER_OUTAGE",
        "review",
        None,
    ),
    (
        "photo",
        "관악 302동",
        "GWANAK_302",
        "복도 벽면에 물 얼룩이 보여요. 크기가 커지는지 확인해 주세요.",
        "ARCH_WATERPROOF",
        "review",
        None,
    ),
    (
        "door",
        "관악 303동",
        "GWANAK_303",
        "출입문 손잡이가 헐거워 문을 여닫기 어렵습니다.",
        "ARCH_DOOR_WINDOW_LOCK",
        "review",
        None,
    ),
    (
        "light",
        "관악 300동",
        "GWANAK_300",
        "복도 조명이 깜빡여 점검이 필요합니다.",
        "ELEC_LIGHTING",
        "confirmed",
        None,
    ),
    (
        "water",
        "관악 301동",
        "GWANAK_301",
        "화장실 세면대 아래에서 물이 떨어집니다.",
        "MECH_WATER_LEAK",
        "progress",
        "worker_b",
    ),
    (
        "floor",
        "관악 302동",
        "GWANAK_302",
        "복도 바닥 마감재가 들떠 있어 걸을 때 걸립니다.",
        "ARCH_FLOOR_WALL_CEILING",
        "report",
        "worker_c",
    ),
    (
        "drain",
        "관악 303동",
        "GWANAK_303",
        "건물 앞 배수로 덮개가 흔들립니다.",
        "CIVIL_DRAINAGE",
        "completed",
        "worker_d",
    ),
    (
        "switch",
        "관악 300동",
        "GWANAK_300",
        "강의실 조명 스위치가 작동하지 않습니다.",
        "ELEC_OUTLET_SWITCH",
        "completed",
        "worker_a",
    ),
)


# Separate, fictional live history; the eight hands-on scenarios remain unseeded.
LIVE_HISTORY = (
    (
        "lh-received",
        "관악 301동",
        "GWANAK_301",
        "301동 2층 복도 창문이 잘 닫히지 않아 찬바람이 들어옵니다.",
        "ARCH_DOOR_WINDOW_LOCK",
        "review",
        None,
        None,
    ),
    (
        "lh-confirmed",
        "관악 60동",
        "GWANAK_60",
        "60동 1층 출입구 조명이 간헐적으로 꺼집니다.",
        "ELEC_LIGHTING",
        "confirmed",
        None,
        None,
    ),
    (
        "lh-queued",
        "관악 62-1동",
        "GWANAK_62-1",
        "62-1동 열람실 출입문 손잡이가 흔들립니다.",
        "ARCH_DOOR_WINDOW_LOCK",
        "queued",
        "worker_c",
        None,
    ),
    (
        "lh-progress",
        "관악 301동",
        "GWANAK_301",
        "301동 3층 화장실 세면대 아래 배관에서 물방울이 떨어집니다.",
        "MECH_WATER_LEAK",
        "progress",
        "worker_b",
        None,
    ),
    (
        "lh-report",
        "관악 60동",
        "GWANAK_60",
        "60동 계단참 천장 조명이 깜빡여 점검을 요청합니다.",
        "ELEC_LIGHTING",
        "report",
        "worker_a",
        "계단참 조명 기구를 점검하고 램프를 교체했습니다. 점등 상태를 확인했습니다.",
    ),
    (
        "lh-completed-a",
        "관악 62-1동",
        "GWANAK_62-1",
        "62-1동 복도 문이 바닥에 닿아 열 때 소리가 납니다.",
        "ARCH_DOOR_WINDOW_LOCK",
        "completed",
        "worker_c",
        "문 경첩을 조정하고 바닥과의 간섭이 없는지 확인했습니다.",
    ),
    (
        "lh-completed-b",
        "관악 301동",
        "GWANAK_301",
        "301동 1층 세면대 수도꼭지에서 잠근 뒤에도 물이 조금씩 떨어집니다.",
        "MECH_WATER_LEAK",
        "completed",
        "worker_b",
        "수도꼭지 내부 부품을 교체하고 누수 여부를 확인했습니다.",
    ),
    (
        "lh-completed-c",
        "관악 60동",
        "GWANAK_60",
        "60동 복도 벽면 스위치가 눌려도 조명이 켜지지 않습니다.",
        "ELEC_OUTLET_SWITCH",
        "completed",
        "worker_a",
        "스위치를 교체하고 해당 조명의 점등을 확인했습니다.",
    ),
)


REPORT_TEXT = "현장을 확인하고 필요한 조치를 진행했습니다."
SHOWCASE_REPORT_INPUTS = frozenset(
    (report.text, report.result)
    for report in (*SHOWCASE_REPORTS.values(), SHOWCASE_FINAL_REPORT)
)
SMOKE_TEXT = "관악 301동 복도 조명을 확인해 주세요."

# Replay-only inputs for the isolated historical seed. Exact pairs only; these
# are deliberately separate from the live allowlist below.
HISTORY_TEMPLATES = (
    (
        "관악 301동",
        "GWANAK_301",
        "ELEC_LIGHTING",
        "worker_a",
        "복도 조명이 어두워 점검을 요청합니다.",
    ),
    ("관악 302동", "GWANAK_302", "MECH_WATER_LEAK", "worker_b", "세면대 배관을 확인해 주세요."),
    (
        "관악 303동",
        "GWANAK_303",
        "ARCH_DOOR_WINDOW_LOCK",
        "worker_c",
        "출입문 손잡이 점검을 요청합니다.",
    ),
    (
        "관악 300동",
        "GWANAK_300",
        "CIVIL_DRAINAGE",
        "worker_d",
        "건물 앞 배수로 덮개를 점검해 주세요.",
    ),
)
HISTORY_MERGE = (
    ("시설관", "2층 화장실 세면대의 물이 샙니다."),
    ("시설관", "같은 화장실 세면대 누수를 확인해 주세요."),
)
HISTORY_EXTRA = (
    ("관악 301동", "분전함에서 연기가 보여 확인을 요청합니다."),
    ("관악 302동", "콘센트에서 스파크가 보여 확인을 요청합니다."),
    ("관악 301동", "교내 다른 부서에 확인이 필요한 시설 문의입니다."),
)


def history_replay_match(text: str, location: str, photo_count: int) -> bool:
    if (
        not isinstance(text, str)
        or not isinstance(location, str)
        or type(photo_count) is not int
        or photo_count != 0
    ):
        return False
    pairs = (
        {(row[0], row[4]) for row in HISTORY_TEMPLATES} | set(HISTORY_EXTRA) | set(HISTORY_MERGE)
    )
    return (" ".join(location.split()), " ".join(text.split())) in pairs


def ticket_replay_fixture(
    text: str, *, history: bool = False, location: str = "", photo_count: int = 0
) -> tuple[str, str]:
    if text == "[합성 예시] 세면대에서 물이 새요":
        return "plumbing-example", "PLUMBING"
    if history and history_replay_match(text, location, photo_count):
        return "booth-history-v1", "UNKNOWN"
    return "unknown", "UNKNOWN"


def allowed_live_input(kind: str, text: str, report_result: str | None) -> bool:
    if kind == "ticket_analysis":
        return report_result is None and text in {row[3] for row in SCENARIOS} | {
            row[3] for row in LIVE_HISTORY
        } | {row[3] for row in SHOWCASE} | {SMOKE_TEXT}
    if kind != "report_analysis":
        return False
    return (
        (text, report_result) in SHOWCASE_REPORT_INPUTS
        or (text == REPORT_TEXT and report_result == "completed")
        or (report_result == "completed" and text in {row[7] for row in LIVE_HISTORY if row[7]})
    )
