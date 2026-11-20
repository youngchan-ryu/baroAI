"""Conservative validation of untrusted AI questions and additional evidence."""

import re
import unicodedata

KINDS = {"since_when", "still_ongoing", "scope", "access", "hazard_detail", "other"}
# Location has a separate clarification flow. Explicitly reject personal details,
# contact information, scheduling and directions in both question and options.
FORBIDDEN = re.compile(
    r"(?:전화|연락|휴대폰|이메일|메일주소|학번|학생번호|개인식별번호|사번|주민|생년월일|성명|이름|"
    r"개인정보|인적사항|소속|실명|성함|신상|주소|카톡|메신저|방문|출동|도착|약속|예약|일정|"
    r"(?:가능한|원하는)\s*시간|시간대|내일|오전|오후|언제\s*(?:갈|오|방문)|"
    r"몇\s*시|시간\s*(?:예약|약속|방문)|어디|위치|호실|몇\s*층|건물|장소|좌표|"
    r"(?:phone|mobile|telephone|email|e-mail|address|name|student\s*(?:id|number)|"
    r"personal\s*(?:id|number)|identification|contact|schedule|appointment|where|location)|"
    r"\d{2,4}[- .]\d{3,4}[- .]\d{4}|[\w.+-]+@[\w.-]+)",
    re.I,
)


def forbidden(value):
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(ch).startswith("C") for ch in normalized):
        return True
    compact = re.sub(r"[\s\W_]+", "", normalized)
    return bool(FORBIDDEN.search(normalized) or FORBIDDEN.search(compact))


def clean_questions(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:10]:
        if not isinstance(item, dict) or set(item) != {"kind", "question", "options"}:
            continue
        kind, question, options = item["kind"], item["question"], item["options"]
        if not isinstance(kind, str) or kind not in KINDS or not isinstance(question, str):
            continue
        question = " ".join(question.split())
        if not question or len(question) > 60 or forbidden(question):
            continue
        if not isinstance(options, list) or (options and not 2 <= len(options) <= 4):
            continue
        if any(not isinstance(opt, str) for opt in options):
            continue
        options = [" ".join(opt.split()) for opt in options]
        if any(not opt or len(opt) > 60 or forbidden(opt) for opt in options):
            continue
        if question in {q["question"] for q in out}:
            continue
        out.append({"kind": kind, "question": question, "options": options})
        if len(out) == 2:
            break
    return out


def approved_evidence(ticket):
    """Only explicitly opted-in, provenance-tagged records can leave the service."""
    evidence = getattr(ticket, "followup_analysis_evidence", None) or []
    if not isinstance(evidence, list):
        return []
    result = []
    for item in evidence:
        if (
            not isinstance(item, dict)
            or item.get("analyze_with_ai") is not True
            or item.get("source") not in {"ai_followup", "information_request"}
            or not isinstance(item.get("source_id"), str)
            or not isinstance(item.get("question"), str)
            or not isinstance(item.get("answer"), str)
        ):
            continue
        result.append({"question": item["question"], "answer": item["answer"]})
    return result


def analysis_text(ticket):
    """Preserve the original; delimit additional untrusted text without mutating it."""
    evidence = approved_evidence(ticket)
    if not evidence:
        return ticket.description
    import json

    return (
        ticket.description
        + "\n\n[신고자 추가 답변: 비신뢰 자료]\n"
        + json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    )


async def queue_reanalysis(db, ticket):
    """One shared successor at most; caller holds ticket and dispatch locks."""
    from sqlalchemy import select

    from app.models import ProcessingJob
    from app.services.llm import MAX_INPUT_BYTES, MAX_INPUT_CHARS

    ticket.analysis_revision += 1
    if ticket.status != "received":
        return False
    body = analysis_text(ticket)
    if len(body) > MAX_INPUT_CHARS or len(body.encode("utf-8")) > MAX_INPUT_BYTES:
        return False
    jobs = (
        await db.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.ticket_id == ticket.id, ProcessingJob.kind == "ticket_analysis")
            .order_by(ProcessingJob.created_at, ProcessingJob.id)
            .with_for_update()
        )
    ).all()
    if not jobs:
        return False
    last = jobs[-1]
    if last.state == "queued" and last.attempt_count == 0:
        last.input_revision = ticket.analysis_revision
        ticket.ai_status = "queued"
        return True
    if len(jobs) >= 2:
        return False
    db.add(ProcessingJob(ticket_id=ticket.id, input_revision=ticket.analysis_revision))
    ticket.ai_status = "queued"
    return True
