import re

import pathlib

import logging
from dataclasses import dataclass
from typing import Dict, Tuple, List, Optional, Any

logging.basicConfig(level=logging.INFO, filename="./logs/log_analyzer.log")
_SUMMARY_LINE       = re.compile(r"(?im)^\s*Properties\s+Considered\s*:\s*(\d+)")
_ASSERTIONS_LINE    = re.compile(r"(?im)^\s*assertions\s*:\s*(\d+)")
_ITEM               = lambda name: re.compile(rf"(?im)^\s*-\s*{name}\s*:\s*(\d+)")
_HARD_ERROR_PATTERNS = [
    re.compile(r"\[ERROR\s*\([^)]+\)\]"),          # [ERROR ] ...
    re.compile(r"^\s*ERROR\s*\(", re.IGNORECASE),  # ERROR  ...
    re.compile(r"^\s*FATAL\b", re.IGNORECASE),
    re.compile(r"\bFalsified\b"),                  
    re.compile(r"\bCounterexample\b", re.IGNORECASE),
]
_STATUS_ZERO        = re.compile(r"Exiting the analysis session with status 0", re.IGNORECASE)

def _parse_summary(txt: str) -> Dict[str, int]:

    counts: Dict[str, int] = {}
    m = _SUMMARY_LINE.search(txt)
    if not m:
        return counts
    counts["considered"] = int(m.group(1))

    m2 = _ASSERTIONS_LINE.search(txt)
    if m2:
        counts["assertions"] = int(m2.group(1))

    for key in ("proven", "cex", "error", "undetermined", "unknown", "covered"):
        mm = _ITEM(key).search(txt)
        if mm:
            counts[key] = int(mm.group(1))

    return counts

def _has_hard_error(txt: str) -> bool:
    return any(p.search(txt) for p in _HARD_ERROR_PATTERNS)

@dataclass
class JGResult:
    status: str                         #pass , fail,no_properties,error
    properties_considered: int = 0
    assertions_proven: int = 0
    covers_covered: int = 0
    errors: int = 0
    raw_summary: str = ""
    issues: List[str] = None


def analyze_jg_log(text: str, exit_code: Optional[int] = None) -> JGResult:
    txt = text or ""
    issues: List[str] = []
    for pat in _HARD_ERROR_PATTERNS:
        for m in pat.finditer(txt):
            s = m.group(0)
            if len(s) > 300:
                s = s[:300] + "..."
            issues.append(s)

    counts = _parse_summary(txt)
    if counts:
        considered  = int(counts.get("considered", 0))
        proven      = int(counts.get("proven", 0))
        err_props   = int(counts.get("error", 0))
        covered     = int(counts.get("covered", 0))  # may be absent

        if considered == 0:
            return JGResult(
                    status="no_properties",
                    properties_considered=0,
                    assertions_proven=0,
                    covers_covered=0,
                    errors=err_props,
                    raw_summary=_extract_summary_block(txt),
                    issues=issues or ["No properties considered (0)."],
            )

        if proven > 0 and err_props == 0:
            return JGResult(
                        status="pass",
                        properties_considered=considered,
                        assertions_proven=proven,
                        covers_covered=covered,
                        errors=err_props,
                        raw_summary=_extract_summary_block(txt),
                        issues=issues or [f"proven={proven}, considered={considered}"],
            )
        return JGResult(
            status="fail",
            properties_considered=considered,
            assertions_proven=proven,
            covers_covered=covered,
            errors=err_props,
            raw_summary=_extract_summary_block(txt),
            issues=issues or [f"Summary: proven={proven}, error={err_props}, considered={considered}"],
        )

    if _has_hard_error(txt):
        return JGResult(
            status="error",
            properties_considered=0,
            assertions_proven=0,
            covers_covered=0,
            errors=len(issues),
            raw_summary="",
            issues=issues or ["Hard error detected (no summary)."],
        )

    if exit_code not in (None, 0) and not _STATUS_ZERO.search(txt):
        return JGResult(
            status="error",
            properties_considered=0,
            assertions_proven=0,
            covers_covered=0,
            errors=1,
            raw_summary="",
            issues=[f"Non-zero exit: {exit_code} (no summary)."],
        )

    return JGResult(
        status="error",
        properties_considered=0,
        assertions_proven=0,
        covers_covered=0,
        errors=0,
        raw_summary="",
        issues=["No summary table found."],
    )

def _extract_summary_block(text: str) -> str:
    m = re.search(r"=+\s*SUMMARY\s*=+.*?(?:\n[-=]+|\Z)", text, flags=re.DOTALL | re.IGNORECASE)
    return m.group(0) if m else ""

def analyze_log(log_path: str, exit_code: Optional[int] = None) -> Tuple[str, List[str], Dict[str, int]]:
    txt = pathlib.Path(log_path).read_text(encoding="utf-8", errors="ignore")
    res = analyze_jg_log(txt, exit_code=exit_code)

    summary_counts = _parse_summary(txt)

    kind = res.status
    if kind == "no_properties":
                kind = "error"  
    return kind, (res.issues or []), summary_counts






