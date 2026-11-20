"""One reporter response to a bounded AI question bundle."""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, model_validator

from app.dispatch_lock import dispatch_lock
from app.followup_questions import queue_reanalysis
from app.information_requests import add_event
from app.location_workflow import refresh_safety
from app.schemas import AIFollowupView, Input
from app.security import authenticate, role
from app.tickets import get_ticket, idempotency, save_idempotency

router = APIRouter(prefix="/v1/tickets/{ticket_id}")


class Answer(Input):
    question_id: Literal["q1", "q2"]
    answer: str = Field(min_length=1, max_length=200)


class FollowupInput(Input):
    action: Literal["answer", "skip"]
    answers: list[Answer] = Field(default_factory=list, max_length=2)
    analyze_with_ai: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def valid_answers(self):
        ids = [a.question_id for a in self.answers]
        if (self.action == "skip" and (ids or self.analyze_with_ai)) or (
            self.action == "answer" and not ids
        ):
            raise ValueError("invalid_followup_answers")
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_followup_answer")
        return self


@router.post("/ai-followup", response_model=AIFollowupView)
async def respond(ticket_id: uuid.UUID, payload: FollowupInput, request: Request):
    async with request.app.state.database.transaction() as db:
        await dispatch_lock(db)
        principal = await authenticate(request, db)
        role(principal, "reporter")
        ticket = await get_ticket(db, ticket_id, principal, lock=True)
        data = ticket.ai_followup_data
        if not data:
            raise HTTPException(404, "ai_followup_not_found")
        scope, digest, prior = await idempotency(
            db,
            request,
            f"ai_followup:{ticket.id}:{principal.actor_scope}",
            payload,
            view_type=AIFollowupView,
        )
        if prior:
            return AIFollowupView.model_validate(data)
        if data.get("status") != "pending":
            raise HTTPException(409, "ai_followup_closed")
        questions = {q["id"]: q for q in data["questions"]}
        if any(answer.question_id not in questions for answer in payload.answers):
            raise HTTPException(422, "unknown_followup_question")
        answers = [answer.model_dump() for answer in payload.answers]
        ticket.ai_followup_data = {
            **data,
            "status": "answered" if answers else "skipped",
            "answers": answers,
        }
        if answers:
            ticket.information_safety_evidence = [
                *(ticket.information_safety_evidence or []),
                *(a["answer"] for a in answers),
            ]
            refresh_safety(ticket)
            ticket.followup_analysis_evidence = [
                *(ticket.followup_analysis_evidence or []),
                *(
                    {
                        "source": "ai_followup",
                        "source_id": a["question_id"],
                        "question": questions[a["question_id"]]["question"],
                        "answer": a["answer"],
                        "analyze_with_ai": payload.analyze_with_ai,
                    }
                    for a in answers
                ),
            ]
            if payload.analyze_with_ai:
                await queue_reanalysis(db, ticket)
        add_event(
            db,
            ticket,
            principal.user,
            "ai_followup_answered" if answers else "ai_followup_skipped",
            "신고자가 추가 질문에 답변" if answers else "신고자가 추가 질문을 건너뜀",
        )
        result = AIFollowupView.model_validate(ticket.ai_followup_data)
        save_idempotency(db, scope, digest, result)
        return result
