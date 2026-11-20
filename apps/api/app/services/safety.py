"""Deterministic raw-text safety signals; never depends on model output."""

import hashlib
import re
from dataclasses import asdict, dataclass
from itertools import permutations

RULE_VERSION = "raw-safety-v4"
# Each pair of adjacent signals is bounded and cannot cross a sentence boundary.
WATER_ELECTRIC_MAX_GAP = 48
_WATER = r"(?<![가-힣A-Za-z0-9_])(?:수돗물|빗물|물)(?=[이가을은의에로도만과와\s.!?,]|$)"
_EQUIPMENT = (
    r"(?:콘센트|전등|조명|형광등|배전(?:함|반)|분전(?:함|반)|"
    r"(?:전기\s*)?(?:배선|전선|전원\s*코드)|"
    r"전기\s*(?:설비|기기|장치|시설|케이블))"
)
_WATER_ACTION = (
    r"(?:떨어(?=지|져|졌|짐)|흘러|흐르|흘렀|샙|샜|"
    r"새(?=요|어|고|는|는데|지|서|며)|젖(?=었|어|고|는|지|은|으|습|게|음))"
)
_SIGNAL_GAP = rf"[^.!?\r\n]{{0,{WATER_ELECTRIC_MAX_GAP}}}?"


def _water_electric_pattern():
    alternatives = []
    signals = {"water": _WATER, "equipment": _EQUIPMENT, "action": _WATER_ACTION}
    for index, order in enumerate(permutations(signals)):
        alternatives.append(_SIGNAL_GAP.join(
            f"(?P<water_action_{index}>{signals[name]})" if name == "action" else signals[name]
            for name in order
        ))
    # Leak/flood words already encode water ingress, so do not require a second verb.
    alternatives.extend([
        rf"(?P<water_action_6>누수|침수){_SIGNAL_GAP}{_EQUIPMENT}",
        rf"{_EQUIPMENT}{_SIGNAL_GAP}(?P<water_action_7>누수|침수)",
    ])
    return re.compile("|".join(alternatives))

# 0 is most urgent. These are internal triage signals, not treatment guarantees.
RULES = [
    ("fire", 0, re.compile(r"불(?:이)?\s*(?:났|나고|나요|나서|붙)|화재|불꽃|연기|타는\s*냄새")),
    ("electric", 0, re.compile(r"감전|스파크|전선.{0,8}(?:노출|나와)|누전|찌릿")),
    ("gas", 0, re.compile(r"가스.{0,5}(?:냄새|누출|새)|가스가\s*새")),
    (
        "collapse",
        0,
        re.compile(r"붕괴|무너|추락|천장(?!에서\s*물이\s*떨어).{0,6}떨어|천장에서\s*잔해가\s*떨어|갇혔|갇혀"),
    ),
    ("flood", 1, re.compile(r"침수|물.{0,6}(?:쏟아|넘쳐)|누수|천장에서\s*물이\s*떨어")),
    ("injury", 0, re.compile(r"다쳤|부상|출혈|피가\s*나|의식.{0,4}없|쓰러")),
    ("fall_risk", 1, re.compile(r"유리.{0,4}깨|깨진\s*유리|난간.{0,6}흔들|천장.{0,6}내려앉")),
    ("construction", 1, re.compile(r"공사.{0,10}(?:위험|쓰러|기울|떨어)|펜스.{0,6}(?:넘어|기울)")),
    ("slip", 1, re.compile(r"미끄러|빙판|물웅덩이")),
    ("elevator_stop", 1, re.compile(r"(?:엘리베이터|승강기).{0,20}(?:멈췄|멈춤|안\s*움직|고장)")),
    (
        "restroom_unusable",
        1,
        re.compile(
            r"(?:화장실|변기).{0,20}(?:전부|모두|다\s*막|전체).{0,6}(?:막|못\s*써|사용\s*불가)"
        ),
    ),
    (
        "water_electric",
        1,
        _water_electric_pattern(),
    ),
]


@dataclass(frozen=True)
class SafetyResult:
    original_hash: str
    rule_version: str
    rule_floor: int
    ai_urgency: int | None
    automatic_urgency: int
    hits: list[dict]

    def as_dict(self):
        return asdict(self)


def rule_hit_id(rule_id: str, start: int, end: int) -> str:
    return f"{rule_id}:{start}:{end}"


def inspect_raw_text(raw: str, ai_urgency: int | None = None) -> SafetyResult:
    if ai_urgency is not None and (isinstance(ai_urgency, bool) or ai_urgency not in range(4)):
        raise ValueError("ai_urgency must be 0..3 or None")
    hits = []
    for rule_id, floor, pattern in RULES:
        for match in pattern.finditer(raw):
            # Context is evidence only. No whole-report negation suppression is allowed.
            start = max(raw.rfind(mark, 0, match.start()) for mark in ".!?\n") + 1
            endings = [raw.find(mark, match.end()) for mark in ".!?\n"]
            ending = min((point for point in endings if point >= 0), default=len(raw))
            clause = raw[start:ending]
            # The verb may precede the equipment. Keep negation after that verb visible
            # even when the full evidence span also contains the later equipment.
            context_anchor = match.end()
            if rule_id == "water_electric":
                context_anchor = next(
                    match.end(name)
                    for name, value in match.groupdict().items()
                    if value is not None
                )
            suffix = raw[context_anchor : min(ending, context_anchor + 24)]
            if re.search(r"없|아니|안\s*(?:나|났)|않|오인", suffix):
                context = "negated"
            elif re.search(r"어제|지난|과거|예전|당시|작년", clause):
                context = "past"
            elif re.search(r"인지|같|의심|모르|아마|혹시", clause):
                context = "ambiguous"
            else:
                context = "current"
            hits.append(
                {
                    "hit_id": rule_hit_id(rule_id, match.start(), match.end()),
                    "rule_id": rule_id,
                    "suggested_urgency": floor,
                    "start": match.start(),
                    "end": match.end(),
                    "context": context,
                }
            )
    floor = min((hit["suggested_urgency"] for hit in hits), default=3)
    return SafetyResult(
        original_hash=hashlib.sha256(raw.encode()).hexdigest(),
        rule_version=RULE_VERSION,
        rule_floor=floor,
        ai_urgency=ai_urgency,
        automatic_urgency=min(ai_urgency if ai_urgency is not None else 3, floor),
        hits=hits,
    )


@dataclass(frozen=True)
class GuardResult:
    urgency: int
    rule_floor: int
    rule_disagreement: bool
    rule_kept: bool
    emergency_banner: bool


def _locally_resolved(raw: str, hit: dict) -> bool:
    """Only explicit local negation or resolved past can justify a downgrade."""
    start, end = hit["start"], hit["end"]
    left = max(raw.rfind(c, 0, start) for c in ".!?\n,;，") + 1
    ends = [raw.find(c, end) for c in ".!?\n,;，"]
    right = min((x for x in ends if x >= 0), default=len(raw))
    before, after = raw[left:start], raw[end:right]
    # Refuse any continuing or current danger in this clause; do not infer scope.
    if re.search(
        r"(?:하지만|그러나|그런데|지금|현재|반면|있지만|없지만|계속|여전히|아직|실제로|다시)",
        before + after,
    ):
        return False
    particle = r"(?:은|는|이|가|을|를|도)?"
    negative = (
        r"(?:없(?:어요|습니다|다|고|으며|음)|아니(?:에요|다|고)|"
        r"안\s*(?:나요|납니다|났어요))(?![가-힣])"
    )
    # Only direct negation or a bounded pair of coordinated hazard terms.
    prefix = (
        r"\s*(?:(?:이나|나|과|와|하고)\s*"
        r"(?:불꽃|연기|타는\s*냄새)\s*)?" + particle + r"\s*"
    )
    negated = bool(re.match(prefix + negative, after))
    if re.search(r"없(?:지|지는|을\s*수)|아니지|않지", after):
        negated = False
    # Bind completion directly to this hit and its event, never another subject.
    past = bool(
        re.search(r"(?:어제|지난|과거|예전|당시|작년)", before)
        and re.fullmatch(
            r"\s*(?:은|는|이|가)?\s*"
            r"(?:(?:났고|났지만|났으나|발생했고|발생했지만)\s*)?"
            r"(?:(?:해결|수리|진화|복구)(?:했어요|했습니다|했다|됐어요|됐습니다|됐다|"
            r"되었어요|되었습니다|되었다)|사라졌어요|사라졌습니다|사라졌다|"
            r"없어졌어요|없어졌습니다|없어졌다)\s*", after
        )
    )
    return negated or past


def has_current_hazard(raw: str, hits: list[dict] | None = None) -> bool:
    """Show 119 advice only for an independent, unresolved rule signal."""
    if hits is None:
        hits = inspect_raw_text(raw).hits
    return any(hit["suggested_urgency"] <= 1 and not _locally_resolved(raw, hit) for hit in hits)


def assess_live_urgency(raw: str, ai_urgency: int, assessments: list[dict]) -> GuardResult:
    """Fail closed unless every more urgent hit has unique, local proof."""
    if type(ai_urgency) is not int or ai_urgency not in range(4):
        raise ValueError("invalid_ai_urgency")
    result = inspect_raw_text(raw)
    needed = [h for h in result.hits if h["suggested_urgency"] < ai_urgency]
    by_id = {}
    for item in assessments if isinstance(assessments, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        hit_id = item.get("hit_id")
        if not isinstance(hit_id, str):
            return GuardResult(min(ai_urgency, result.rule_floor), result.rule_floor,
                               bool(needed), bool(needed),
                               has_current_hazard(raw, result.hits))
        by_id.setdefault(hit_id, []).append(item)
    valid_ids = {h["hit_id"] for h in result.hits}
    valid = set(by_id) <= valid_ids and all(len(items) == 1 for items in by_id.values())
    for hit in needed:
        items = by_id.get(hit["hit_id"], [])
        if len(items) != 1:
            valid = False
            continue
        item = items[0]
        evidence = item.get("evidence")
        if (item.get("status") not in {"negated", "unrelated"}
                or not isinstance(evidence, str) or not 0 < len(evidence) <= 40):
            valid = False
            continue
        # Evidence must cover this occurrence, even if the same words recur elsewhere.
        positions = [m.start() for m in re.finditer(re.escape(evidence), raw)]
        if not any(pos <= hit["start"] and pos + len(evidence) >= hit["end"]
                   for pos in positions):
            valid = False
        if not _locally_resolved(raw, hit):
            valid = False
    kept = bool(needed) and not valid
    return GuardResult(
        urgency=min(ai_urgency, result.rule_floor) if kept else ai_urgency,
        rule_floor=result.rule_floor, rule_disagreement=bool(needed), rule_kept=kept,
        emergency_banner=has_current_hazard(raw, result.hits),
    )
