import json
import re
import pandas as pd
from pathlib import Path
from textwrap import dedent
from typing import Dict, Any, List

from config import resolve_llm
from openai import OpenAI

GENERATED_DIR = Path("./generated")
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
import pandas as pd
from pathlib import Path
from textwrap import dedent

GENERATED_DIR = Path("./generated"); GENERATED_DIR.mkdir(parents=True, exist_ok=True)

def roles_excel_to_analysis(*, roles_xlsx: str, function_name: str, property_name: str) -> str:
    xl = pd.ExcelFile(roles_xlsx)
    sheet = function_name if function_name in xl.sheet_names else xl.sheet_names[0]
    df = pd.read_excel(roles_xlsx, sheet_name=sheet)
    df.columns = [c.strip().lower() for c in df.columns]
    rows = []
    for _, r in df.iterrows():
        role   = str(r.get("role","")).strip()
        chosen = str(r.get("chosen","")).strip()
        if role and chosen:
            rows.append((role, chosen))
    lines = [
        "| role | candidate(s) in design | chosen | width | notes |",
        "| ---  | ---                     | ---    | ---   | ---   |",
    ]
    for role, chosen in rows:
        lines.append(f"| {role} | {chosen} | `{chosen}` |  | selected |")
    body = dedent(f"""
    Roles from pruned AES pairs (sheet={sheet})

    ROLE TABLE
    ----------
    {'\n'.join(lines)}

    NOTES
    -----
    Derived from final_roles workbook.
    """).strip()
    out = GENERATED_DIR / f"{property_name}_static_analysis.txt"
    out.write_text(body, encoding="utf-8")
    return str(out)

def _role_table(rows: List[Dict[str, Any]]) -> str:
    lines = [
        "| role | candidate(s) in design | chosen | width | notes |",
        "| ---  | ---                     | ---    | ---   | ---   |",
    ]
    for r in rows:
        cands = ", ".join(map(str, r.get("candidates", []))) or "-"
        chosen = str(r.get("chosen", "") or "")
        width  = str(r.get("width", "") or "")
        notes  = (r.get("notes") or "static-llm").replace("\n", " ")
        lines.append(f"| {r['role']} | {cands} | `{chosen}` | {width} | {notes} |")
    return "\n".join(lines)

def _normalize_legal_set(x: Any) -> str:
    if isinstance(x, list):
        toks = []
        for t in x:
            t = str(t).strip()
            if not t:
                continue
            t = t.split("=")[0].strip()
            toks.append(t)
        return ", ".join(dict.fromkeys(toks))  
    s = str(x or "").strip()
    if not s:
        return ""
    s = re.sub(r"^\{|\}$", "", s)
    toks = []
    for t in s.split(","):
        t = t.strip()
        if not t:
            continue
        t = t.split("=")[0].strip()
        toks.append(t)
    return ", ".join(dict.fromkeys(toks))

def _family_requirements(family: str, property_name: str) -> Dict[str, Any]:
    fam = family.upper()
    prop = (property_name or "").lower()

    if fam == "RSA":
        return {
            "roles": [
                {"id": "input_txt",  "dir": "input",  "width": 32, "required": True},
                {"id": "output_txt", "dir": "output", "width": 32, "required": True},
            ],
            "needs_legal_set": False,
            "needs_safe_state": False,
            "clock_hint": True, "reset_hint": True,
        }

    if fam == "SHA":
        return {
            "roles": [
                {"id": "text_out", "dir": "output", "width": 32, "required": True},
            ],
            "needs_legal_set": False,
            "needs_safe_state": False,
            "clock_hint": True, "reset_hint": True,
        }

    if fam == "FSM":
        if prop == "always_legal_state":
            return {
                "roles": [
                    {"id": "clk",   "dir": "any", "width": "any", "required": True},
                    {"id": "reset", "dir": "any", "width": "any", "required": True},
                    {"id": "state", "dir": "any", "width": "any", "required": True},
                    {"id": "legal_set", "dir": "n/a", "width": "n/a", "required": True},
                ],
                "needs_legal_set": True,
                "needs_safe_state": False,
                "clock_hint": True, "reset_hint": True,
            }
        if prop == "recovery_from_illegal_state":
            return {
                "roles": [
                    {"id": "clk",   "dir": "any", "width": "any", "required": True},
                    {"id": "reset", "dir": "any", "width": "any", "required": True},
                    {"id": "state", "dir": "any", "width": "any", "required": True},
                    {"id": "next_state", "dir": "any", "width": "any", "required": True},
                    {"id": "safe_state", "dir": "n/a", "width": "n/a", "required": True},
                    {"id": "legal_set", "dir": "n/a", "width": "n/a", "required": True},
                ],
                "needs_legal_set": True,
                "needs_safe_state": True,
                "clock_hint": True, "reset_hint": True,
            }
        return {
            "roles": [{"id":"state","dir":"any","width":"any","required":True}],
            "needs_legal_set": False,
            "needs_safe_state": False,
            "clock_hint": True, "reset_hint": True,
        }

    return {"roles": [], "needs_legal_set": False, "needs_safe_state": False,
            "clock_hint": True, "reset_hint": True}

def _build_prompt(family: str, property_name: str, df: pd.DataFrame, function_name: str, reqs: Dict[str, Any]) -> str:
    sample = df.copy()
    keep = [c for c in ["Variable Name","Type","Bit Width","PDG_Depth","Num_Operators","Centroid"] if c in sample.columns]
    if keep:
        sample = sample[keep]
    sample_text = sample.to_csv(index=False)

    role_text = "\n".join(
        [f"- role `{r['id']}`: dir={r.get('dir','any')}, width={r.get('width','any')} (required={r.get('required',False)})"
         for r in reqs.get("roles", [])]
    ) or "(no fixed roles; pick the best signals you judge relevant)"

    fsm_extraction_notes = """
FSM SPECIAL INSTRUCTIONS
- Detect `clk` / `reset` by common names (clk, clk_i, rst, rst_i, reset, reset_n). Report both strings even if widths are blank.
- Choose the `state` signal (registered state). Often named `state`, `cur_state`, etc.
- If requested, choose `next_state` (combinational next). Often named `next`, `next_state`.
- Extract the LEGAL ENUM SET from the `Type` column if it embeds text like: 
    enum{Q_IDLE=2'd0,Q_LOAD=2'd1,Q_RUN=2'd3,Q_WRAP=2'd2}fsm_v2.qstate_t
  → Convert to a clean list of literals:
    ["Q_IDLE","Q_LOAD","Q_RUN","Q_WRAP"]
- If enum text is unavailable, infer legal states from rows whose Type looks like 'fsm_state' or names like Q_*.
- If SAFE state is requested, prefer a literal matching /(IDLE|RESET|SAFE)/i; otherwise pick the first element of the legal set.
- Return `legal_set` as a JSON array of literal identifiers; we'll format it.
""".strip() if family.upper() == "FSM" else ""

    return f"""
You are selecting **real design signals** for SVA generation using only the table below (from static analysis).

FAMILY: {family}
FUNCTION (sheet): {function_name}
PROPERTY: {property_name}

CANDIDATE SIGNALS (CSV):
{sample_text}

SELECTION INSTRUCTIONS
----------------------
Choose signals that best satisfy the role constraints:

{role_text}

General rules:
- Use only names from the table. Do NOT invent helper signals.
- If you need "previous value", downstream template will use $past() — do NOT create prev_* variables.
- If 'dir' is given, match 'Type' when possible (input/output/inout). If 'width' is given, match 'Bit Width'.
- Prefer exact matches; if not available, choose the closest and explain briefly.

{fsm_extraction_notes}

Return STRICT JSON:

{{
  "roles": [
    {{"role": "<id>", "chosen": "<signal_name or literal>", "width": <int or 0>, 
      "candidates": ["<alt1>","<alt2>"], "notes": "<why>"}}
  ],
  "clock": "<clk_name or empty>",
  "reset": "<rst_name or empty>",
  "legal_set": ["STATE1","STATE2"],      // only if applicable
  "safe_state": "IDLE"                   // only if applicable
}}
""".strip()

def _call_llm(prompt: str, llm_choice: str = "default") -> Dict[str, Any]:
    cfg = resolve_llm(llm_choice)
    client = OpenAI(api_key=cfg.get("api_key") or None, base_url=cfg.get("api_base") or None)
    model = cfg["model"]
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role":"user","content":prompt}],
        temperature=0.2 if not model.lower().startswith(("o1","o3","gpt-5")) else None
    )
    txt = resp.choices[0].message.content or "{}"
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        txt = txt.split("\n",1)[1] if "\n" in txt else txt
    try:
        return json.loads(txt)
    except Exception:
        import re
        m = re.search(r"\{[\s\S]*\}\s*$", txt)
        return json.loads(m.group(0)) if m else {}

def static_analysis_from_excel_llm(
    *, family: str, function_name: str, pdg_excel_path: str, property_name: str, llm_choice: str = "default"
) -> str:

    xl = pd.ExcelFile(pdg_excel_path)
    sheet = function_name if function_name in xl.sheet_names else xl.sheet_names[0]
    df = pd.read_excel(pdg_excel_path, sheet_name=sheet)
    df.columns = [c.strip() for c in df.columns]

    reqs   = _family_requirements(family, property_name)
    prompt = _build_prompt(family, property_name, df, sheet, reqs)
    out    = _call_llm(prompt, llm_choice=llm_choice) or {}

    roles       = out.get("roles", []) or []
    clock_hint  = (out.get("clock") or "").strip()
    reset_hint  = (out.get("reset") or "").strip()
    legal_set   = out.get("legal_set", [])
    safe_state  = out.get("safe_state", "")

    legal_set_str = _normalize_legal_set(legal_set)
    if legal_set_str:
        roles.append({
            "role": "legal_set",
            "chosen": legal_set_str,
            "width": "",
            "candidates": [],
            "notes": "enum literals",
        })
    if safe_state:
        roles.append({
            "role": "safe_state",
            "chosen": str(safe_state),
            "width": "",
            "candidates": [],
            "notes": "SAFE state",
        })

    table = _role_table(roles)

    states_line = f"states = {{{legal_set_str}}}" if legal_set_str else "(no legal_set extracted)"

    body = dedent(f"""
    Best module for {family}:{sheet} (LLM from Excel) → <n/a>

    ROLE TABLE
    ----------
    {table}

    HINTS
    -----
    clock={clock_hint or "<none>"}
    reset={reset_hint or "<none>"}
    {states_line}

    NOTES
    -----
    This analysis file was produced by an LLM that looked only at the Excel sheet (no RTL text).
    Do not invent helper signals; generators will use $past(...) when needed.
    """).strip()

    out_path = GENERATED_DIR / f"{property_name}_static_analysis.txt"
    out_path.write_text(body, encoding="utf-8")
    return str(out_path)

