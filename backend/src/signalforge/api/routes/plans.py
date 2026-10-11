from pathlib import Path

import yaml
from fastapi import APIRouter
from pydantic import ValidationError

from signalforge.api.schemas import PlanExample
from signalforge.config import BACKEND_DIR
from signalforge.domain.plan import load_plan

router = APIRouter(tags=["plans"])

EXAMPLES_DIR = BACKEND_DIR / "examples"
SUFFIX = ".plan.yaml"


@router.get("/plans/examples", response_model=list[PlanExample])
def plan_examples() -> list[PlanExample]:
    """Reviewed example plans to seed the research form (the planner stage is not built)."""
    examples = []
    for path in sorted(EXAMPLES_DIR.rglob(f"*{SUFFIX}")):
        try:
            plan = load_plan(path)
        except (OSError, yaml.YAMLError, ValidationError):
            continue
        id_ = path.relative_to(EXAMPLES_DIR).as_posix().removesuffix(SUFFIX)
        examples.append(PlanExample(id=id_, title=_title(path) or id_, plan=plan))
    return examples


def _title(path: Path) -> str | None:
    """The file's first comment line."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip() or None
        if line.strip():
            return None
    return None
