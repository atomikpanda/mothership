from __future__ import annotations

from mship.core.spec import OpenQuestion, Spec


def add_question(spec: Spec, text: str) -> OpenQuestion:
    """Append a question with the next q<n> id (max existing + 1)."""
    nums = [int(q.id[1:]) for q in spec.open_questions
            if q.id.startswith("q") and q.id[1:].isdigit()]
    q = OpenQuestion(id=f"q{(max(nums) + 1) if nums else 1}", text=text)
    spec.open_questions.append(q)
    return q


def answer_question(spec: Spec, q_id: str, answer: str) -> Spec:
    for q in spec.open_questions:
        if q.id == q_id:
            q.answer = answer
            return spec
    valid = ", ".join(q.id for q in spec.open_questions) or "(none)"
    raise ValueError(f"no open question {q_id!r}; valid ids: {valid}")


def list_questions(spec: Spec) -> list[dict]:
    return [{"id": q.id, "text": q.text, "answer": q.answer} for q in spec.open_questions]
