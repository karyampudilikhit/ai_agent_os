"""The first concrete Vision AI employee (README Phase 10): takes a raw
startup idea and produces market research, competitor analysis, a
feasibility assessment, and a clear-eyed go/no-go verdict — the
narrowest end-to-end useful slice of the product, and the template for
how every later employee type gets built.

Built entirely on the generic Employee base — nothing role-specific
happens outside build_objective().
"""

from __future__ import annotations

from backend.app.employees.employee import Employee


class IdeaValidationEmployee(Employee):
    role = "Idea Validation"
    # The whole pitch is "doesn't fabricate evidence" — verification must
    # never be optional here, regardless of what the generic router
    # thinks a validation-shaped prompt's stakes are. Found the hard way:
    # a real run got classified single_call and skipped critique entirely.
    min_tier = "single_call_critique"

    def build_objective(self, task: str) -> str:
        context = self.memory.relevant_context(task)
        history_block = (
            f"\n\nThis founder has brought you ideas before — relevant history:\n"
            f"{context}\n"
            "If this idea is a revision of something above, say so explicitly "
            "and note what changed."
            if context
            else ""
        )
        return f"""A founder has a raw startup idea and needs it validated before they
commit real time or money to it. Their idea, in their own words:

"{task}"

Produce a validation assessment covering:
1. Market research — how big is this opportunity, realistically, and who
   actually has this problem?
2. Competitor analysis — who already solves this, how well, and what's
   the actual gap this idea fills (if any)?
3. Feasibility — can one person or a small team actually build and
   distribute this, given normal solo-founder resources?
4. A clear-eyed verdict — go, no-go, or go-with-changes, and exactly why.
   Do not hedge. If the idea is weak, say so directly instead of listing
   generic risks and calling it balanced.

Do not fabricate specific statistics, survey results, or "research
already conducted" — if you don't have a real number, describe the
market in qualitative terms instead of inventing a percentage or dollar
figure to sound rigorous.{history_block}"""
