import os
import re
import json
import logging
import pathlib
from typing import Optional, Tuple, List

from .sva_generator import _resolve_llm_settings, _llm_json  # reuse robust LLM stack

__all__ = ["repair_wrapper"]

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
GENERATED_DIR = os.path.join(ROOT, "generated")
os.makedirs(GENERATED_DIR, exist_ok=True)

def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""

def _write(path: str, text: str) -> None:
    pathlib.Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def _strip_code_fences(s: str) -> str:
    s = re.sub(r"(?s)```[\w+-]*\s*", "", s)
    s = s.replace("```", "")
    s = re.sub(r"(?s)~~~[\w+-]*\s*", "", s)
    s = s.replace("~~~", "")
    return s

def _ensure_property_body_semicolon(s: str) -> str:
    return re.sub(
        r"(property\s+[a-zA-Z_]\w*\s*;\s*)([^;]*?)(\s*endproperty)",
        lambda m: m.group(1)
        + (m.group(2).rstrip() + ";" if m.group(2).strip() and not m.group(2).strip().endswith(";") else m.group(2))
        + m.group(3),
        s,
        flags=re.DOTALL,
    )

def _extract_top_from_tcl(tcl_text: str) -> Optional[str]:
    m = re.search(r"elaborate\s+-top\s+([A-Za-z_]\w*)", tcl_text)
    return m.group(1) if m else None

def _has_property_block(sv: str) -> bool:
    return re.search(r"\bproperty\s+[A-Za-z_]\w*\s*;[\s\S]*?endproperty", sv, flags=re.IGNORECASE) is not None

def _has_assertion(sv: str) -> bool:
    return re.search(r"\bassert\s+property\s*\(", sv, flags=re.IGNORECASE) is not None

def _extract_property_bodies(sv: str) -> List[str]:
    return re.findall(r"property\s+[A-Za-z_]\w*\s*;([\s\S]*?)endproperty", sv, flags=re.IGNORECASE)

def _looks_tautology(body: str) -> bool:
    has_ident = re.search(r"\b([A-Za-z_]\w*)\b", body) is not None
    return (not has_ident) and re.search(r"\b(1'b1|1|true)\b", body, flags=re.IGNORECASE) is not None

def _validate_wrapper_sv(wrapper_sv_core: str) -> Tuple[bool, str]:
    if not _has_property_block(wrapper_sv_core) or not _has_assertion(wrapper_sv_core):
        return False, "missing 'property ... endproperty' and/or 'assert property(...)'"
    bodies = _extract_property_bodies(wrapper_sv_core)
    if not bodies:
        return False, "no property body found"
    if all(_looks_tautology(b) for b in bodies):
        return False, "tautological property (e.g., 1'b1)"
    endmods = list(re.finditer(r"\bendmodule\b", wrapper_sv_core, flags=re.IGNORECASE))
    asserts = list(re.finditer(r"\bassert\s+property\s*\(", wrapper_sv_core, flags=re.IGNORECASE))
    if endmods and asserts and asserts[-1].start() > endmods[-1].start():
        return False, "assertion appears after endmodule"
    return True, ""

_WRAPPER_JSON_SPEC = """
Return strictly this JSON object:
{
  "top_module": "<string: the wrapper module name>",
  "wrapper_sv": "<string: complete SystemVerilog text for the wrapper module only (no `include`)>",
  "notes": "<optional: short reasoning>"
}
"""

def _build_repair_prompt(
    design_filename: str,
    design_text: str,
    property_name: str,
    failing_wrapper_sv: str,
    failing_top_guess: Optional[str],
    jg_log_tail: str,
    analysis_text: Optional[str],
) -> str:

    return f"""
You are repairing a *formal verification wrapper* for property "{property_name}".
Infer the correct DUT and ports purely from the design source. Do not assume specific signal names.
Return JSON only (see spec). STRICT requirements:

- The wrapper must declare all needed logic with correct widths/types and instantiate the correct DUT.
- Include **exactly one** SVA property with a single assertion labeled `_assert_1:`.
- Place the property and the assertion **immediately before the wrapper's `endmodule`**.
- The property must be **meaningful** for "{property_name}" (do not emit a tautology such as `1'b1`).
- Prefer posedge real clocks if present; otherwise sample combinationally. Use `disable iff (reset)` correctly.
- The property BODY must end with a semicolon before `endproperty`.
- No hierarchical references to non-existent signals.
- No code fences/backticks in the JSON string fields.

Lightweight analysis (read-only; may be heuristic):
---------------------------------------------------
{(analysis_text or '').strip()}

Design file (read-only): {design_filename}

SystemVerilog design source (read-only):
---------------------------------------
{design_text}
---------------------------------------

Failing wrapper (read-only):
----------------------------
{failing_wrapper_sv}
----------------------------

JasperGold log tail (read-only) {(f'(previous -top: {failing_top_guess})' if failing_top_guess else '')}:
---------------------------------------------------------------------
{jg_log_tail}
---------------------------------------------------------------------

{_WRAPPER_JSON_SPEC}
""".strip()

def _build_fixup_prompt(base_prompt: str, reason: str, previous_wrapper_sv: str) -> str:
    return (
        base_prompt
        + f"""

VALIDATION FEEDBACK (read carefully and correct):
- Your previous repair failed validation because: {reason}.
- You MUST return JSON whose "wrapper_sv":
  * Includes **exactly one** 'property ... endproperty' block
  * Includes **one** '_assert_1: assert property (...)'
  * Both appear **immediately before** 'endmodule'
  * Property is **not** a tautology (no '1'b1' etc.)

Previous repair attempt (read-only):
-----------------------------------
{previous_wrapper_sv}
-----------------------------------
"""
    )

def repair_wrapper(
    *,
    property_name: str,
    failing_wrapper_sv_path: str,
    failing_tcl_path: str,
    jg_log_text: str,
    analysis_text: Optional[str],
    attempt_idx: int,
    design_src_path: str,
    llm_choice: Optional[str] = None,
) -> Tuple[str, str, str]:
    if not os.path.exists(design_src_path):
        raise FileNotFoundError(f"Design source not found: {design_src_path}")

    design_text = _read(design_src_path)
    design_base = os.path.basename(design_src_path)
    design_stub = os.path.splitext(design_base)[0]

    failing_sv = _read(failing_wrapper_sv_path)
    failing_tcl = _read(failing_tcl_path) if failing_tcl_path else ""
    prev_top = _extract_top_from_tcl(failing_tcl)

    jg_tail = jg_log_text[-8000:] if jg_log_text else ""

    base_prompt = _build_repair_prompt(
            design_filename=design_base,
            design_text=design_text,
            property_name=property_name,
            failing_wrapper_sv=failing_sv,
            failing_top_guess=prev_top,
            jg_log_tail=jg_tail,
            analysis_text=analysis_text,
    )

    llm_settings = _resolve_llm_settings(llm_choice)

    max_tries = int(os.environ.get("REPAIR_MAX_TRIES", "3"))
    last_sv = ""
    reason = "initial repair"
    for attempt in range(1, max_tries + 1):
        prompt = base_prompt if attempt == 1 else _build_fixup_prompt(base_prompt, reason, last_sv)
        out = _llm_json(prompt, llm_settings=llm_settings)

        wrapper_sv_core = _strip_code_fences(out.get("wrapper_sv") or "")
        top = out.get("top_module") or ""
        wrapper_sv_core = _ensure_property_body_semicolon(wrapper_sv_core)

        if not top:
            m = re.search(r"\bmodule\s+([A-Za-z_]\w*)\s*\(", wrapper_sv_core)
            if m:
                top = m.group(1)

        ok, reason = _validate_wrapper_sv(wrapper_sv_core)
        if ok and top:
            base = os.path.basename(failing_wrapper_sv_path)
            name_wo_ext = re.sub(r"\.sv$", "", base)
            fixed_sv_name = f"{design_stub}_{name_wo_ext}_repair{attempt_idx}.sv"
            fixed_sv_path = os.path.join(GENERATED_DIR, fixed_sv_name)

            full_wrapper_text = f'`include "{design_base}"\n\n{wrapper_sv_core.strip()}\n'
            _write(fixed_sv_path, full_wrapper_text)

            fixed_tcl_name = f"{design_stub}_{property_name}_repair{attempt_idx}.tcl"
            fixed_tcl_path = os.path.join(GENERATED_DIR, fixed_tcl_name)
            fixed_tcl = f"""clear -all

analyze -sv12 {os.path.basename(fixed_sv_path)}

elaborate -top {top} -create_related_covers witness

clock -none
reset -none

prove -all
"""
            _write(fixed_tcl_path, fixed_tcl)

            logger.info("Repaired wrapper written: %s (top=%s)", fixed_sv_path, top)
            logger.info("Repaired TCL written    : %s", fixed_tcl_path)
            return fixed_sv_path, fixed_tcl_path, top

        last_sv = wrapper_sv_core or (out.get("wrapper_sv") or "")
        dbg = os.path.join(GENERATED_DIR, f"{design_stub}_{os.path.basename(failing_wrapper_sv_path).replace('.sv','')}_repair{attempt_idx}_attempt{attempt}_invalid.sv")
        if last_sv:
            _write(dbg, last_sv)

    raise RuntimeError(f"Repair output invalid after {max_tries} tries: {reason}")
