import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


RULES: Dict[str, Dict[str, Any]] = {
    "top_module": {
        "roles": [
            {"id": "clk",      "kind": "hint"},
            {"id": "rst",      "kind": "hint"},
            {"id": "message",  "kind": "signal",
             "width_any_of": [32, 64, 128, 256, 512, 1024, 2048],
             "dir_any_of": ["input"]},
            {"id": "cipher",   "kind": "signal",
             "width_any_of": [32, 64, 128, 256, 512, 1024, 2048],
             "dir_any_of": ["output"]},
        ],
    },
}


def _maybe_import_llm():
    from config import resolve_llm
    from openai import OpenAI
    return resolve_llm, OpenAI
def _sdi_bigram_sim(a: str, b: str) -> float:
    a = (a or "").lower()
    b = (b or "").lower()
    if not a or not b:
        return 0.0

    def bigr(s: str):
        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}

    A = bigr(a)
    B = bigr(b)
    if not A or not B:
        return 0.0
    return 2.0 * len(A & B) / (len(A) + len(B))


def _sdi_distance(name: str, role_id: str) -> float:
    return 1.0 - _sdi_bigram_sim(name, role_id)

def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    if "Bit Width" in df.columns:
        df["Bit Width"] = pd.to_numeric(df["Bit Width"], errors="coerce")
    if "Type" not in df.columns:
        df["Type"] = ""
    if "Variable Name" not in df.columns:
        for alt in ("Signal", "Name", "Var"):
            if alt in df.columns:
                df = df.rename(columns={alt: "Variable Name"})
                break
    return df


def _read_metrics(xlsx: str, sheet: str) -> pd.DataFrame:
    xl = pd.ExcelFile(xlsx)
    use_sheet = sheet if sheet in xl.sheet_names else xl.sheet_names[0]
    df = pd.read_excel(xlsx, sheet_name=use_sheet)
    return _normalize_columns(df)


def _read_pairs(int_xlsx: str, sheet: str) -> pd.DataFrame:
    xl = pd.ExcelFile(int_xlsx)
    if sheet not in xl.sheet_names:
        return pd.DataFrame()

    df = pd.read_excel(int_xlsx, sheet_name=sheet)
    df = _normalize_columns(df)

    if "Variable Pair" not in df.columns:
        return pd.DataFrame()

    sp = df["Variable Pair"].astype(str).str.split(" - ", n=1, expand=True)
    df["main_var"] = sp[0].fillna("")
    df["assist_var"] = sp[1].fillna("")

    if "Thr" not in df.columns:
        for alt in ("thr", "Threshold", "Score"):
            if alt in df.columns:
                df = df.rename(columns={alt: "Thr"})
                break
    return df

def _dir_bucket(s: str) -> str:
    s = (s or "").lower()
    if "input" in s:
        return "input"
    if "output" in s:
        return "output"
    if "inout" in s:
        return "inout"
    if "wire" in s:
        return "wire"
    return "logic"


def _filter_candidates(df_main: pd.DataFrame,
                       df_pairs: pd.DataFrame,
                       role_rule: Dict[str, Any]) -> pd.DataFrame:

    need_widths = role_rule.get("width_any_of")
    need_dirs = [d.lower() for d in role_rule.get("dir_any_of", [])]

    paired = set(df_pairs["main_var"].astype(str))
    df = df_main[df_main["Variable Name"].astype(str).isin(paired)].copy()

    df["_dir"] = df["Type"].astype(str).map(_dir_bucket)

    if need_widths:
        df = df[df["Bit Width"].isin(need_widths)]
    if need_dirs:
        df = df[df["_dir"].isin(need_dirs)]

    return df


def _min_thr_by_var(df_pairs: pd.DataFrame) -> pd.Series:
    if "Thr" not in df_pairs.columns:
        return pd.Series(dtype=float)
    return df_pairs.groupby("main_var")["Thr"].min()
def _is_synth_node(name: str) -> bool:
    s = str(name)
    return s.startswith("cond_") or s.startswith("case_")


def _label_variants(*, design_label: str, design1_excel: str, int_pairs: str) -> List[str]:
    cands = set()

    def add(x: str):
        x = (x or "").strip()
        if x:
            cands.add(x)

    add(design_label)

    add(Path(design1_excel).stem.replace("_output", ""))
    m = re.search(r"INT_RSA_(?P<label>.+?)_", Path(int_pairs).name, flags=re.I)
    if m:
        add(m.group("label"))

    for x in list(cands):
        add(re.sub(r"([a-zA-Z]+)(\d+)$", r"\1_\2", x))  
        add(x.replace("_", ""))                         
        add(f"{x}_output")
        add(f"{x.replace('_','')}_output")

    return sorted(set(cands))


def _extract_nodes_map(pack: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(pack, dict):
        return {}
    nodes = pack.get("nodes")
    if isinstance(nodes, dict):
        return nodes
    pdg = pack.get("pdg")
    if isinstance(pdg, dict):
        return pdg
    return {}


def _extract_reverse_edges(pack: Dict[str, Any], nodes: Dict[str, Any]) -> Dict[str, List[str]]:
    rev = pack.get("reverse_edges")
    if isinstance(rev, dict):
        out = {}
        for k, v in rev.items():
            out[str(k)] = [str(x) for x in v] if isinstance(v, list) else []
        return out

    rev_built: Dict[str, List[str]] = {}
    for tgt, nd in nodes.items():
        conns = nd.get("connections", []) if isinstance(nd, dict) else []
        for src in conns or []:
            s = str(src)
            rev_built.setdefault(s, []).append(str(tgt))
    for k in list(rev_built.keys()):
        rev_built[k] = sorted(set(rev_built[k]))
    return rev_built


def _fanin_fanout(pack: Optional[Dict[str, Any]], var: str) -> Tuple[int, int]:
    if not pack or not var:
        return 0, 0
    nodes = _extract_nodes_map(pack)
    if not nodes:
        return 0, 0
    rev = _extract_reverse_edges(pack, nodes)

    conns = []
    if var in nodes and isinstance(nodes[var], dict):
        conns = nodes[var].get("connections", []) or []

    fanin_set = {str(x) for x in conns if x and not _is_synth_node(str(x))}
    fanout_set = {str(x) for x in (rev.get(var, []) or []) if x and not _is_synth_node(str(x))}
    return len(fanin_set), len(fanout_set)


def _fan_score_from_role(role_rule: Dict[str, Any], fanin: int, fanout: int) -> float:
    need_dirs = [d.lower() for d in role_rule.get("dir_any_of", [])]
    wants_input = "input" in need_dirs
    wants_output = "output" in need_dirs

    if wants_input and not wants_output:
        return float(fanout - fanin)
    if wants_output and not wants_input:
        return float(fanin - fanout)
    return float(fanin + fanout)


def _extract_message_from_pack(pack: Optional[Dict[str, Any]], dbg_prefix: str = "") -> Optional[str]:
    if not pack or not isinstance(pack, dict):
        print(f"{dbg_prefix}[subPDG] no pack loaded, cannot extract message role")
        return None

    nodes = _extract_nodes_map(pack)
    roles = pack.get("roles") if isinstance(pack.get("roles"), dict) else {}
    role_by_var = pack.get("role_by_var") if isinstance(pack.get("role_by_var"), dict) else {}

    tagged = sorted({str(name) for name, nd in nodes.items()
                     if isinstance(nd, dict) and str(nd.get("role", "")).strip().lower() == "message"})
    if tagged:
        print(f"{dbg_prefix}[subPDG] node-tag role==message found: {tagged} (using {tagged[0]})")
        return tagged[0]

    msg_list = []
    if isinstance(roles, dict):
        x = roles.get("message")
        if isinstance(x, list):
            msg_list = [str(v) for v in x if v]
    if msg_list:
        print(f"{dbg_prefix}[subPDG] roles.message found: {msg_list} (using {msg_list[0]})")
        return msg_list[0]

    rbv = sorted({str(k) for k, v in role_by_var.items()
                  if str(v).strip().lower() == "message"})
    if rbv:
        print(f"{dbg_prefix}[subPDG] role_by_var==message found: {rbv} (using {rbv[0]})")
        return rbv[0]

    print(f"{dbg_prefix}[subPDG] message role NOT FOUND in pack.")
    if nodes:
        any_roles = sorted({str(nd.get("role", "")) for nd in nodes.values()
                            if isinstance(nd, dict) and nd.get("role")})
        print(f"{dbg_prefix}[subPDG] node-tag roles present: {any_roles or '<none>'}")
    if isinstance(roles, dict) and roles:
        print(f"{dbg_prefix}[subPDG] pack.roles keys: {sorted(list(roles.keys()))}")
    if isinstance(role_by_var, dict) and role_by_var:
        items = list(role_by_var.items())[:10]
        print(f"{dbg_prefix}[subPDG] role_by_var sample (first 10): {items}")
    return None


def _try_load_json(p: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _best_pack_from_candidates(cands: List[Path],
                               *,
                               func: str,
                               labels: List[str],
                               expect_design_file: str) -> Optional[Path]:
    if not cands:
        return None

    for p in cands:
        pack = _try_load_json(p)
        if not isinstance(pack, dict):
            continue
        if str(pack.get("function", "")).strip().lower() != func.strip().lower():
            continue
        if str(pack.get("design_file", "")).strip().lower() == expect_design_file.lower():
            return p
    for p in cands:
        s = p.name.lower()
        if s.endswith(f"__{func.lower()}.json"):
            if any(lab.lower() in s for lab in labels if lab):
                return p

    for p in cands:
        if p.name.lower().endswith(f"__{func.lower()}.json"):
            return p

    return cands[0]


def _load_pdg_pack(pdg_json_dir: Optional[str],
                   *,
                   design_label: str,
                   func: str,
                   design1_excel: str,
                   int_pairs: str,
                   debug: bool = True) -> Optional[Dict[str, Any]]:
    labels = _label_variants(design_label=design_label, design1_excel=design1_excel, int_pairs=int_pairs)
    expect_design_file = f"{Path(design1_excel).stem.replace('_output','')}.json"

    if pdg_json_dir:
        d = Path(pdg_json_dir)
        if d.is_dir():
            for lab in labels:
                for pat in (
                    f"pdg__{lab}__{func}.json",
                    f"pdg__{lab.replace('_output','')}__{func}.json",
                    f"pdg__{lab}_output__{func}.json",
                    f"pdg__{lab.replace('_','')}__{func}.json",
                ):
                    p = d / pat
                    if p.exists():
                        pack = _try_load_json(p)
                        if pack is not None:
                            if debug:
                                print(f"[PDG] {design_label}:{func}: loaded {p}")
                            return pack

            globs = []
            for lab in labels:
                globs += list(d.glob(f"pdg__*{lab}*__{func}.json"))
            globs = sorted(set(globs))
            if globs:
                pick = _best_pack_from_candidates(globs, func=func, labels=labels, expect_design_file=expect_design_file)
                pack = _try_load_json(pick) if pick else None
                if pack is not None:
                    if debug:
                        print(f"[PDG] {design_label}:{func}: glob-loaded {pick}")
                    return pack
        else:
            if debug:
                print(f"[PDG] {design_label}:{func}: --pdg_json_dir is not a dir: {d}")

    root = Path(".")
    candidates = sorted(root.rglob(f"pdg__*__{func}.json"))
    if debug:
        print(f"[PDG] {design_label}:{func}: auto-find scanning project for pdg__*__{func}.json -> {len(candidates)} files")

    pick = _best_pack_from_candidates(candidates, func=func, labels=labels, expect_design_file=expect_design_file)
    if not pick:
        if debug:
            print(f"[PDG] {design_label}:{func}: auto-find could not locate a PDG pack")
        return None

    pack = _try_load_json(pick)
    if pack is None:
        if debug:
            print(f"[PDG] {design_label}:{func}: auto-find picked {pick} but failed to parse JSON")
        return None

    if debug:
        print(f"[PDG] {design_label}:{func}: auto-find loaded {pick} (design_file={pack.get('design_file')})")
    return pack

def _print_candidates(design_label: str, func: str, role_id: str, df: pd.DataFrame) -> None:
    print(f"\n[CANDIDATES] {design_label}:{func}:{role_id}  (sorted: Thr, SDI, FanScore, PDG_Depth)")
    if df is None or df.empty:
        print("  <no candidates>")
        return

    cols_pref = [
        "Variable Name", "Type", "Bit Width", "Thr", "SDI",
        "FanIn", "FanOut", "FanScore", "PDG_Depth"
    ]
    cols = [c for c in cols_pref if c in df.columns]
    with pd.option_context("display.max_rows", None,
                           "display.max_columns", None,
                           "display.width", 200):
        print(df[cols].to_string(index=False))


def _print_final_selections(design_label: str, func: str, df_roles: pd.DataFrame) -> None:
    print(f"\n[FINAL] {design_label}:{func} selections")
    if df_roles is None or df_roles.empty:
        print("  <no selections>")
        return

    for _, r in df_roles.iterrows():
        role = str(r.get("Role", ""))
        chosen = str(r.get("Chosen", ""))
        notes = str(r.get("Notes", ""))
        print(f"  - {role:8s} -> {chosen}  ({notes})")


def _build_llm_prompt(func: str,
                      role_defs: List[Dict[str, Any]],
                      cand_by_role: Dict[str, pd.DataFrame],
                      thr_by_var: pd.Series,
                      df_main_metrics: pd.DataFrame) -> str:
    blocks: List[str] = []

    for r in role_defs:
        if r["kind"] != "signal":
            continue
        rid = r["id"]
        df = cand_by_role.get(rid, pd.DataFrame())
        if df.empty:
            blocks.append(f"ROLE {rid}: (no candidates)")
            continue

        df = df.copy()
        df["Thr"] = df["Variable Name"].map(thr_by_var).fillna(1e9)

        keep = [c for c in
                ["Variable Name", "Type", "Bit Width", "Thr", "SDI", "PDG_Depth", "Centroid", "FanIn", "FanOut", "FanScore"]
                if c in df.columns]
        blocks.append(f"ROLE {rid} CANDIDATES (CSV)\n" + df[keep].to_csv(index=False))

    names_all = df_main_metrics["Variable Name"].astype(str).tolist()
    clk_hint = ", ".join([n for n in names_all if re.search(r"\bclk\b", n, re.I)]) or "<none>"
    rst_hint = ", ".join([n for n in names_all if re.search(r"\brst|reset\b", n, re.I)]) or "<none>"

    req_text = "\n".join(
        [f"- `{r['id']}`: width∈{r.get('width_any_of', 'any')}, dir∈{r.get('dir_any_of', 'any')}"
         for r in role_defs if r["kind"] == "signal"]
    ) or "(no strict roles)"

    return f"""
You must choose **one signal per role** for RSA {func} using only the candidates shown.

Role constraints (from the property template):
{req_text}

Clock/reset name hints found in design: clk={clk_hint}  reset={rst_hint}

{chr(10).join(blocks)}

Selection policy:
- Prefer smallest Thr.
- If tied/close on Thr, prefer lower SDI (name similarity to role id).
- Then prefer higher FanScore.
- If still tied, prefer larger PDG_Depth when available.

Return STRICT JSON:
{{
  "roles": [
    {{"role":"<id>","chosen":"<Variable Name or empty>","notes":"<short reason>"}}
  ],
  "clock": "<clk_name or empty>",
  "reset": "<rst_name or empty>"
}}
""".strip()


def _call_llm(prompt: str, llm_choice: str) -> Dict[str, Any]:
    resolve_llm, OpenAI = _maybe_import_llm()
    cfg = resolve_llm(llm_choice)
    client = OpenAI(api_key=cfg.get("api_key") or None, base_url=cfg.get("api_base") or None)
    model = cfg["model"]
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1
    )
    txt = (resp.choices[0].message.content or "{}").strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        txt = txt.split("\n", 1)[1] if "\n" in txt else txt
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{[\s\S]*\}\s*$", txt)
        return json.loads(m.group(0)) if m else {}

def _lookup_feats(cand_by_role: Dict[str, pd.DataFrame], role: str, chosen: str) -> Dict[str, Any]:
    df = cand_by_role.get(role, pd.DataFrame())
    if df.empty or not chosen:
        return {"FanIn": "", "FanOut": "", "FanScore": "", "SDI": ""}
    hit = df[df["Variable Name"].astype(str) == str(chosen)]
    if hit.empty:
        return {"FanIn": "", "FanOut": "", "FanScore": "", "SDI": ""}
    row = hit.iloc[0]
    return {
        "FanIn": row.get("FanIn", ""),
        "FanOut": row.get("FanOut", ""),
        "FanScore": row.get("FanScore", ""),
        "SDI": row.get("SDI", ""),
    }


def _roles_json_to_df(func: str, out_json: Dict[str, Any], cand_by_role: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for r in (out_json.get("roles") or []):
        role = r.get("role", "")
        chosen = r.get("chosen", "")
        feats = _lookup_feats(cand_by_role, role, chosen)
        rows.append({
            "Function": func,
            "Role": role,
            "Chosen": chosen,
            "Notes": r.get("notes", ""),
            **feats
        })

    clk =(out_json.get("clock") or "").strip()
    rst =(out_json.get("reset") or "").strip()
    if clk:
        rows.append({"Function": func, "Role": "clk", "Chosen": clk, "Notes": "hint",
                     "FanIn": "", "FanOut": "", "FanScore": "", "SDI": ""})
    if rst:
        rows.append({"Function": func, "Role": "rst", "Chosen": rst, "Notes": "hint",
                     "FanIn": "", "FanOut": "", "FanScore": "", "SDI": ""})
    return pd.DataFrame(rows)


def _greedy_pick(func: str,
                    role_defs: List[Dict[str, Any]],
                    cand_by_role: Dict[str, pd.DataFrame],
                    thr_by_var: pd.Series) -> pd.DataFrame:
    rows = []
    for r in role_defs:
        if r["kind"] != "signal":
            continue
        rid = r["id"]
        dfr = cand_by_role.get(rid, pd.DataFrame())

        choice = ""
        note = "no candidates"
        feats = {"FanIn": "", "FanOut": "", "FanScore": "", "SDI": ""}

        if not dfr.empty:
            d = dfr.copy()
            d["Thr"] = d["Variable Name"].map(thr_by_var).fillna(1e9)
            if "SDI" not in d.columns:
                d["SDI"] = d["Variable Name"].astype(str).map(lambda n: _sdi_distance(n, rid))

            sort_cols = ["Thr", "SDI"]
            asc = [True, True]

            if "FanScore" in d.columns:
                sort_cols.append("FanScore")
                asc.append(False)

            if "PDG_Depth"  in d.columns:
                sort_cols.append("PDG_Depth")
                asc.append(False)

            d = d.sort_values(sort_cols, ascending=asc, na_position="last").reset_index(drop=True)

            choice = str(d.iloc[0]["Variable Name"])
            note = f"min Thr={float(d.iloc[0]['Thr']):.6g}, SDI={float(d.iloc[0]['SDI']):.3g}"
            if "FanScore" in d.columns and not pd.isna(d.iloc[0].get("FanScore", None)):
                note += f", FanScore={float(d.iloc[0]['FanScore']):.3g}"

            feats ={
                    "FanIn":d.iloc[0].get("FanIn", ""),
                    "FanOut": d.iloc[0].get("FanOut", ""),
                    "FanScore": d.iloc[0].get("FanScore", ""),
                    "SDI": d.iloc[0].get("SDI", ""),
            }

        rows.append({"Function": func, "Role": rid, "Chosen": choice, "Notes": note, **feats})

    return pd.DataFrame(rows)

def _dedupe_roles(df_roles: pd.DataFrame,
                  cand_by_role: Dict[str, pd.DataFrame],
                  lock_roles: Optional[List[str]] = None) -> pd.DataFrame:
    lock_roles = [str(x) for x in (lock_roles or [])]

    rows = df_roles[df_roles["Role"].isin(cand_by_role.keys())].copy()
    used: Dict[str, List[str]] = {}
    for role, name in rows[["Role", "Chosen"]].itertuples(index=False):
        used.setdefault(name, []).append(role)

    collisions = {name: roles for name, roles in used.items() if name and len(roles) > 1}
    if not collisions:
        return df_roles

    def rank(role: str, var: str) -> int:
        df = cand_by_role.get(role, pd.DataFrame())
        if df.empty:
            return math.inf
        try:
            idx = df.index[df["Variable Name"].astype(str) == str(var)][0]
            return int(idx)
        except Exception:
            return math.inf

    chosen_map = {r: n for r, n  in rows[["Role", "Chosen"]].itertuples(index=False)}
    taken = set([n for n in chosen_map.values() if n])

    for name, roles in collisions.items():
        locked = [r for r in roles if r in lock_roles]
        if locked:
            winner =locked[0]
        else:
            winner= min(roles, key=lambda r: rank(r, name))

        losers = [r for r in roles if r != winner]

        for r in losers:
            if r in lock_roles:
                continue
            df = cand_by_role.get(r, pd.DataFrame())
            repl = None
            if not df.empty:
                for _, rr in df.iterrows():
                    v = str(rr["Variable Name"])
                    if v not in taken:
                        repl = v
                        break
            if repl:
                chosen_map[r] = repl
                taken.add(repl)

    out = df_roles.copy()
    out.loc[out["Role"].isin(chosen_map.keys()), "Chosen"] = out["Role"].map(chosen_map)
    return out

def _module_from_int(xl_pairs: pd.ExcelFile, func: str) -> str:
    try:
        if "Modules" not in xl_pairs.sheet_names:
            return ""
        dfm = pd.read_excel(xl_pairs, sheet_name="Modules")
        cols = {c.lower(): c for c in dfm.columns}
        fcol = cols.get("function")
        d1col = cols.get("design1_module") or cols.get("design_1_module") or cols.get("d1_module")
        if not fcol or not d1col:
            return ""
        m = dfm[dfm[fcol].astype(str).str.strip().str.lower() == func.strip().lower()]
        if m.empty:
            return ""
        return str(m.iloc[0][d1col]).strip()
    except Exception:
        return ""

def run_single_int(*,
                   int_pairs: str,
                   design1_excel: str,
                   design_label: str,
                   functions: List[str],
                   llm_choice: str,
                   select_with_llm: bool,
                   out_xlsx: str,
                   pdg_json_dir: Optional[str] = None) -> bool:
    try:
        xl_pairs = pd.ExcelFile(int_pairs)
    except Exception as e:
        print(f"[SKIP] {design_label}: cannot open INT workbook {int_pairs}: {e}")
        return False

    try:
        xl_main = pd.ExcelFile(design1_excel)
    except Exception as e:
        print(f"[SKIP] {design_label}: cannot open metrics {design1_excel}: {e}")
        return False

    all_sheets: Dict[str, pd.DataFrame] = {}

    for func in functions:
        rule = RULES.get(func)
        if not rule:
            print(f"[SKIP] {design_label}:{func}:  unknown function.")
            continue
        if func not in xl_pairs.sheet_names:
            print(f"[SKIP] {design_label}:{func}: sheet missing in INT.")
            continue
        if func not in xl_main.sheet_names:
            print(f"[SKIP] {design_label}:{func}: sheet missing in metrics.")
            continue

        df_metrics = _read_metrics(design1_excel, func)
        df_pairs = _read_pairs(int_pairs, func)
        if df_pairs.empty or df_metrics.empty:
            print(f"[SKIP] {design_label}:{func}: empty metrics/pairs.")
            continue

        thr_by_var = _min_thr_by_var(df_pairs)

        pack = _load_pdg_pack(
            pdg_json_dir,
            design_label=design_label,
            func=func,
            design1_excel=design1_excel,
            int_pairs=int_pairs,
            debug=True,
        )

        subpdg_message = _extract_message_from_pack(pack, dbg_prefix=f"[{design_label}:{func}] ")

        cand_by_role: Dict[str, pd.DataFrame] = {}
        for role in rule["roles"]:
            if role["kind"] != "signal":
                continue

            rid = role["id"]
            dfr = _filter_candidates(df_metrics, df_pairs, role)

            if not dfr.empty:
                dfr = dfr.copy()
                dfr["Thr"] = dfr["Variable Name"].map(thr_by_var).fillna(1e9)
                dfr["SDI"] = dfr["Variable Name"].astype(str).map(lambda n: _sdi_distance(n, rid))

                fanin_list = []
                fanout_list = []
                fscore_list = []
                for v in dfr["Variable Name"].astype(str).tolist():
                    fi, fo = _fanin_fanout(pack, v)
                    fanin_list.append(fi)
                    fanout_list.append(fo)
                    fscore_list.append(_fan_score_from_role(role, fi, fo))

                dfr["FanIn"] = fanin_list
                dfr["FanOut"] = fanout_list
                dfr["FanScore"] = fscore_list

                sort_cols = ["Thr", "SDI"]
                asc = [True, True]
                if "FanScore" in dfr.columns:
                    sort_cols.append("FanScore")
                    asc.append(False)
                if "PDG_Depth" in dfr.columns:
                    sort_cols.append("PDG_Depth")
                    asc.append(False)
                dfr = dfr.sort_values(sort_cols, ascending=asc, na_position="last").reset_index(drop=True)

            cand_by_role[rid] = dfr

        for role in rule["roles"]:
            if role["kind"] != "signal":
                continue
            rid = role["id"]
            _print_candidates(design_label, func, rid, cand_by_role.get(rid, pd.DataFrame()))

        if select_with_llm:
            prompt = _build_llm_prompt(func, rule["roles"], cand_by_role, thr_by_var, df_metrics)
            out_json = _call_llm(prompt, llm_choice) or {}
            df_roles = _roles_json_to_df(func, out_json, cand_by_role=cand_by_role)
        else:
            df_roles = _greedy_pick(func, rule["roles"], cand_by_role, thr_by_var)

        if df_roles.empty:
            print(f"[SKIP] {design_label}:{func}: no selections produced.")
            continue

        if subpdg_message:
            idxs = df_roles.index[df_roles["Role"].astype(str) == "message"].tolist()
            if idxs:
                idx = idxs[0]
                prev = str(df_roles.at[idx, "Chosen"])
                df_roles.at[idx, "Chosen"] = subpdg_message
                df_roles.at[idx, "Notes"] = f"FORCED from subPDG (role==message). was={prev}"
            else:
                df_roles = pd.concat([df_roles, pd.DataFrame([{
                    "Function": func,
                    "Role": "message",
                    "Chosen": subpdg_message,
                    "Notes": "FORCED from subPDG (role==message)",
                    "FanIn": "",
                    "FanOut": "",
                    "FanScore": "",
                    "SDI": "",
                }])], ignore_index=True)

            print(f"[OVERRIDE] {design_label}:{func}: forced message -> {subpdg_message}")
            df_roles = _dedupe_roles(df_roles, cand_by_role, lock_roles=["message"])
        else:
            print(f"[OVERRIDE] {design_label}:{func}: no subPDG message found; keeping Thr-based selection")

        dut_name = _module_from_int(xl_pairs, func)
        if dut_name:
            df_roles = pd.concat([
                df_roles,
                pd.DataFrame([{
                    "Function": func,
                    "Role": "dut",
                    "Chosen": dut_name,
                    "Notes": "from INT.Modules (Design1)",
                    "FanIn": "",
                    "FanOut": "",
                    "FanScore": "",
                    "SDI": "",
                }])
            ], ignore_index=True)

        _print_final_selections(design_label, func, df_roles)
        all_sheets[func] = df_roles

    if not all_sheets:
        print(f"[INFO] {design_label}: no functions produced selections; nothing to write.")
        return False

    Path(out_xlsx).parent.mkdir(parents=True, exist_ok=True)
    tmp = str(Path(out_xlsx).with_suffix(".tmp.xlsx"))

    try:
        with pd.ExcelWriter(tmp, engine="openpyxl") as w:
            for name, df in all_sheets.items():
                df.to_excel(w, sheet_name=name[:31], index=False)
        Path(tmp).replace(out_xlsx)
    except PermissionError as e:
        print(f"[ERROR] Permission denied writing {out_xlsx}: {e}. Is the file open in Excel?")
        try:
            if Path(tmp).exists():
                Path(tmp).unlink()
        except Exception:
            pass
        return False
    except Exception as e:
        print(f"[ERROR] Writing {out_xlsx} failed: {e}")
        try:
            if Path(tmp).exists():
                Path(tmp).unlink()
        except Exception:
            pass
        return False

    print(f"[OK] {design_label}: wrote final roles → {out_xlsx}")
    return True

def main():
    ap = argparse.ArgumentParser(description="RSA pair-pruning → role selection + HARD subPDG message override + auto-find PDG pack")

    ap.add_argument("--int_pairs", help="INT workbook from pairwise step (single run)")
    ap.add_argument("--design1_excel", help="Main design metrics (xlsx with RSA sheets)")
    ap.add_argument("--design_label", help="Main design label (used for output name)")
    ap.add_argument("--out", help="Output Excel for single run")
    ap.add_argument("--int_dir", help="Folder containing INT workbooks (batch mode)")
    ap.add_argument("--glob", default=None, help="Glob to match INT files (default: INT_RSA_*_{assistant_name}.xlsx)")
    ap.add_argument("--assistant_name", default="assis_rsa", help="Assistant design name")
    ap.add_argument("--design1_excel_dir", default="static_var/RSA", help="Where to find per-design metrics in batch")
    ap.add_argument("--out_dir", default="static_mod/RSA", help="Where to write final_roles_*.xlsx in batch")
    ap.add_argument("--functions", default="top_module", help="Comma list of RSA function names/sheets to process.")
    ap.add_argument("--llm_choice", default="default")
    ap.add_argument("--no_llm", action="store_true",
                    help="Disable LLM and pick deterministically (Thr + SDI + FanScore)")

    ap.add_argument("--pdg_json_dir", default=None,
                    help="Folder containing pdg__<label>__<func>.json PDG packs (optional; auto-find used if wrong)")

    args = ap.parse_args()
    functions = [s.strip() for s in args.functions.split(",") if s.strip()]
    select_with_llm = not args.no_llm

    if not args.glob:
        args.glob = f"INT_RSA_*_{args.assistant_name}.xlsx"

    label_pattern = f"INT_RSA_{{label}}_{args.assistant_name}.xlsx"
    if args.int_dir:
        int_dir = Path(args.int_dir)
        if not int_dir.is_dir():
            raise FileNotFoundError(f"--int_dir not found: {int_dir}")

        pat = re.escape(label_pattern).replace(r"\{label\}", r"(?P<label>.+)")
        rx = re.compile("^" + pat + "$", re.I)

        count = 0
        for p in sorted(int_dir.glob(args.glob)):
            m = rx.match(p.name)
            if not m:
                print(f"[SKIP] cannot extract label from filename: {p.name}")
                continue

            label = m.group("label")

            metrics1 = Path(args.design1_excel_dir) / f"{label}_output.xlsx"
            if not metrics1.exists():
                alt = Path(args.design1_excel_dir) / f"{label}.xlsx"
                metrics1 = alt if alt.exists() else metrics1

            if not metrics1.exists():
                print(f"[SKIP] {label}: metrics not found at {metrics1} (or <label>.xlsx).")
                continue

            out_xlsx = Path(args.out_dir) / f"final_roles_{label}.xlsx"
            ok = run_single_int(
                int_pairs=str(p),
                design1_excel=str(metrics1),
                design_label=label,
                functions=functions,
                llm_choice=args.llm_choice,
                select_with_llm=select_with_llm,
                out_xlsx=str(out_xlsx),
                pdg_json_dir=args.pdg_json_dir,
            )
            count += 1 if ok else 0

        if count == 0:
            print(f"[INFO] No INT files matched {args.glob} in {int_dir}")
        return

    if not (args.int_pairs and args.design1_excel):
        raise ValueError("For single run, provide --int_pairs and --design1_excel (or use --int_dir for batch).")

    label = args.design_label or Path(args.design1_excel).stem.replace("_output", "")
    out_xlsx = args.out or f"static_mod/RSA/final_roles_{label}.xlsx"

    run_single_int(
        int_pairs=args.int_pairs,
        design1_excel=args.design1_excel,
        design_label=label,
        functions=functions,
        llm_choice=args.llm_choice,
        select_with_llm=select_with_llm,
        out_xlsx=out_xlsx,
        pdg_json_dir=args.pdg_json_dir,
    )


if __name__ == "__main__":
    main()




