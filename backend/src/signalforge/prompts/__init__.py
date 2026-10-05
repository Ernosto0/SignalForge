"""Versioned prompts: ``prompts/<id>.md`` with a YAML header (id, version).

Bump ``version`` whenever the wording changes meaningfully; it is recorded on every LLMCall and is
part of the LLM cache key, so old cached answers are not reused for a new prompt.
"""

from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel

PROMPTS_DIR = Path(__file__).parent


class Prompt(BaseModel):
    id: str
    version: int
    text: str

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


@cache
def load_prompt(prompt_id: str) -> Prompt:
    raw = (PROMPTS_DIR / f"{prompt_id}.md").read_text(encoding="utf-8")
    _, header, body = raw.split("---", 2)
    meta = yaml.safe_load(header)
    if meta.get("id") != prompt_id:
        raise ValueError(f"prompt {prompt_id}.md declares id {meta.get('id')!r}")
    return Prompt(id=prompt_id, version=int(meta["version"]), text=body.strip())
