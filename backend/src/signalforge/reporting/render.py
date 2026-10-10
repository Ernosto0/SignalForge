"""Render ``report.json`` (source of truth) to Markdown and HTML with Jinja2, like the landscape
report. Every bullet links to the claims it cites; hypotheses, assumptions and recommendations are
labelled; quotes stay in the original language with their English translation."""

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from signalforge.config import ReportDefaults
from signalforge.reporting.schema import Report
from signalforge.reporting.sections import TITLES

TEMPLATES = Path(__file__).parent / "templates"
KIND_LABEL = {
    "fact": None,
    "inference": "Inference",
    "hypothesis": "Hypothesis",
    "assumption": "Assumption",
    "recommendation": "Recommendation",
}


def report_dir(cfg: ReportDefaults, run_id: int) -> Path:
    """Where a run's report files go (next to the landscape report's ``run-<id>``)."""
    return cfg.out_dir / f"run-{run_id}"


def make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(enabled_extensions=("html.j2",), default=False),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = lambda v: "–" if v is None else f"{v:.0%}"
    return env


def _label(bullet: dict[str, Any], claims: dict[int, dict[str, Any]]) -> str | None:
    label = KIND_LABEL[bullet["kind"]]
    if bullet["kind"] == "assumption" and any(
        claims[i]["meta"].get("sourced") is False for i in bullet["claim_ids"] if i in claims
    ):
        label = "Assumption, unsourced"
    return label


def view(report: Report) -> dict[str, Any]:
    """The report as plain data plus what the templates need (labels, resolved citations)."""
    data = json.loads(report.model_dump_json())
    claims = {c["id"]: c for c in data["claims"]}

    def decorate(bullets: list[dict[str, Any]]) -> None:
        for b in bullets:
            b["label"] = _label(b, claims)
            b["refs"] = [claims[i] for i in b["claim_ids"] if i in claims]

    decorate(data["summary"])
    for opp in data["opportunities"]:
        for section in opp["sections"]:
            section["title"] = TITLES[section["key"]]
            decorate(section["bullets"])
    cited = {i for b in data["summary"] for i in b["claim_ids"]}
    for opp in data["opportunities"]:
        cited |= {i for s in opp["sections"] for b in s["bullets"] for i in b["claim_ids"]}
    data["claims"] = [c for c in data["claims"] if c["id"] in cited]  # Sources: cited claims only
    return data


def render_report(report: Report, out_dir: Path) -> list[Path]:
    """Write report.json / .md / .html into ``out_dir``; returns the written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    env = make_env()
    written = []
    path = out_dir / "report.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    written.append(path)
    data = view(report)
    for ext in ("md", "html"):
        path = out_dir / f"report.{ext}"
        path.write_text(env.get_template(f"report.{ext}.j2").render(r=data), encoding="utf-8")
        written.append(path)
    return written
