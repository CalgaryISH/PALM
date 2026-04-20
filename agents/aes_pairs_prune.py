 ### must pass --no_llm for step3

import argparse
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
RULES: Dict[str, Dict[str, Any]] = {
    "AddRoundKey": {
        "roles": [
            {"id": "clk", "kind": "hint"},
            {"id": "reset", "kind": "hint"},
            {"id": "state_in", "kind": "signal", "width_any_of": [128], "dir_any_of": ["input"]},
            {"id": "round_key", "kind": "signal", "width_any_of": [128], "dir_any_of": ["input"]},
            {"id": "state_out", "kind": "signal", "width_any_of": [128], "dir_any_of": ["output"]},
        ],
    },
    "SBox": {
        "roles": [
            {"id": "clk", "kind": "hint"},
            {"id": "reset", "kind": "hint"},
            {"id": "sbox_in", "kind": "signal", "width_any_of": [8], "dir_any_of": ["input"]},
            {"id": "sbox_out", "kind": "signal", "width_any_of": [8], "dir_any_of": ["output"]},
        ],
    },
    "ShiftRows": {
        "roles": [
            {"id": "clk", "kind": "hint"},
            {"id": "reset", "kind": "hint"},
            {"id": "state_in", "kind": "signal", "width_any_of": [128], "dir_any_of": ["input"]},
            {"id": "state_out", "kind": "signal", "width_any_of": [128], "dir_any_of": ["output"]},
        ],
    },
    "KeyExpansion": {
        "roles": [
            {"id": "clk", "kind": "hint"},
            {"id": "reset", "kind": "hint"},
            {"id": "key_in", "kind": "signal", "width_any_of": [128, 192, 256], "dir_any_of": ["input"]},
            {"id": "round_key_out", "kind": "signal", "width_any_of": [128], "dir_any_of": ["output"]},
        ],
    },
}


def _env_truthy(s: Optional[str]) -> bool:
    return str(s or "").strip().lower() in {"1", "true", "yes", "y", "on"}

def _maybe_import_llm():
    from config import resolve_llm
    from openai import OpenAI

    return resolve_llm, OpenAI
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


def _min_thr_by_var(df_pairs: pd.DataFrame) -> pd.Series:
    if "Thr" not in df_pairs.columns:
        return pd.Series(dtype=float)
    return df_pairs.groupby("main_var")["Thr"].min()


def _width_miss(bw: Any, widths_req: Optional[List[int]]) -> float:
    if not widths_req:
        return 0.0
    try:
        if pd.isna(bw):
            return 1e6
        bwi = int(bw)
        if bwi in widths_req:
            return 0.0
        return float(min(abs(bwi - int(w)) for w in widths_req))
    except Exception:
        return 1e6


def _dir_miss(dir_bucket: str, dirs_req: Optional[List[str]]) -> float:
    if not dirs_req:
        return 0.0
    want = [d.lower() for d in dirs_req]
    return 0.0 if dir_bucket in want else 1.0


def _build_candidates_scored(
    df_metrics: pd.DataFrame,
    df_pairs: pd.DataFrame,
    role_rule: Dict[str, Any],
    thr_by_var: pd.Series,
) -> pd.DataFrame:
    paired = set(df_pairs["main_var"].astype(str))
    df = df_metrics[df_metrics["Variable Name"].astype(str).isin(paired)].copy()
    if df.empty:
        return df

    df["_dir"] = df["Type"].astype(str).map(_dir_bucket)
    df["Thr"] = df["Variable Name"].map(thr_by_var).fillna(1e9)

    widths_req = role_rule.get("width_any_of", None)
    dirs_req = role_rule.get("dir_any_of", None)

    df["DirMiss"] = df["_dir"].map(lambda d: _dir_miss(d, dirs_req))
    df["WidthMiss"] = df["Bit Width"].map(lambda bw: _width_miss(bw, widths_req))

    sort_cols: List[str] = ["DirMiss", "WidthMiss", "Thr"]
    asc: List[bool] = [True, True, True]

    if "PDG_Depth" in df.columns:
        sort_cols.append("PDG_Depth")
        asc.append(False)

    df = df.sort_values(sort_cols, ascending=asc, na_position="last").reset_index(drop=True)
    return df


def _build_llm_prompt(
    func: str,
    role_defs: List[Dict[str, Any]],
    cand_by_role: Dict[str, pd.DataFrame],
    thr_by_var: pd.Series,
    df_main_metrics: pd.DataFrame,
) -> str:
    blocks: List[str] = []

    for r in role_defs:
        if r["kind"] != "signal":
            continue
        rid = r["id"]
        df = cand_by_role.get(rid, pd.DataFrame())
        if df.empty:
            blocks.append(f"ROLE {rid}: (no candidates)")
            continue
        keep = [c for c in ["Variable Name", "Type", "Bit Width", "Thr", "DirMiss", "WidthMiss", "PDG_Depth"] if c in df.columns]
        blocks.append(f"ROLE {rid} CANDIDATES (CSV)\n" + df[keep].to_csv(index=False))

    names_all = df_main_metrics["Variable Name"].astype(str).tolist()
    clk_hint = ", ".join([n for n in names_all if re.search(r"\bclk\b", n, re.I)]) or "<none>"
    rst_hint = ", ".join([n for n in names_all if re.search(r"\brst(_n)?\b|reset", n, re.I)]) or "<none>"

    req_text = "\n".join(
        [
            f"- {r['id']}: width∈{r.get('width_any_of','any')}, dir∈{r.get('dir_any_of','any')}"
            for r in role_defs
            if r["kind"] =="signal"
        ]
    ) or "(no strict roles)"

    return f"""
You must choose **one signal per role** for AES {func} using only the candidates shown.

Role constraints:
{req_text}

Clock/reset name hints found in design:
clk={clk_hint}
reset={rst_hint}

{chr(10).join(blocks)}

Prefer candidates with DirMiss=0 and WidthMiss=0, then smallest Thr.
If a role has no candidates, leave it blank.

Return STRICT JSON:
{{
  "roles": [ {{"role":"<id>","chosen":"<Variable Name or empty>","notes":"<short reason>"}} ],
  "clock": "<clk_name or empty>",
  "reset": "<reset_name or empty>"
}}
""".strip()


def _call_llm(prompt: str,llm_choice: str) -> Dict[str, Any]:
    resolve_llm, OpenAI = _maybe_import_llm()
    cfg = resolve_llm(llm_choice)
    client = OpenAI(api_key=cfg.get("api_key") or None, base_url=cfg.get("api_base") or None)
    model = cfg["model"]
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
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

def _roles_json_to_df(func: str, out_json: Dict[str, Any]) -> pd.DataFrame:
    rows = []
    for r in (out_json.get("roles") or []):
        rows.append(
            {
                "Function":func,
                "Role":r.get("role", ""),
                "Chosen": r.get("chosen", ""),
                "Notes": r.get("notes", ""),
            }
        )

    clk = (out_json.get("clock") or "").strip()
    rst = (out_json.get("reset") or "").strip()
    if clk:
        rows.append({"Function": func, "Role": "clk","Chosen": clk, "Notes": "hint"})
    if rst:
        rows.append({"Function": func,"Role": "reset", "Chosen": rst, "Notes": "hint"})
    return pd.DataFrame(rows)


def _greedy_pick(
    func: str,
    role_defs: List[Dict[str, Any]],
    cand_by_role: Dict[str, pd.DataFrame],
) -> pd.DataFrame:
    rows = []
    for r in role_defs:
        if r["kind"] != "signal":
            continue
        rid = r["id"]
        dfr = cand_by_role.get(rid, pd.DataFrame())

        choice = ""
        note = "no candidates"

        if not dfr.empty:
            top = dfr.iloc[0]
            choice = str(top.get("Variable Name", ""))
            note = (
                f"DirMiss={float(top.get('DirMiss', 999)):g}, "
                f"WidthMiss={float(top.get('WidthMiss', 999)):g}, "
                f"Thr={float(top.get('Thr', 1e9)):.6g}"
            )

        rows.append({"Function": func, "Role": rid, "Chosen": choice, "Notes": note})

    return pd.DataFrame(rows)


def _append_clk_reset_hints(df_roles: pd.DataFrame, df_metrics: pd.DataFrame, func: str) -> pd.DataFrame:
    out = df_roles.copy()
    roles_lower = set(out["Role"].astype(str).str.lower().tolist())

    names = df_metrics["Variable Name"].astype(str).tolist()

    def pick_clk() -> str:
        exact = [n for n in names if  n.lower() == "clk"]
        if exact:
            return exact[0]
        cand = [n for n in names if re.search(r"\bclk\b|clock", n, re.I)]
        return cand[0] if cand else ""

    def pick_rst() -> str:
        exact = [n for n in names if n.lower() in {"rst", "rst_n", "reset"}]
        if exact:
            return exact[0]
        cand = [n for n in names  if re.search(r"\brst(_n)?\b|reset", n, re.I)]
        return cand[0] if cand else ""

    if "clk" not in roles_lower:
        clk = pick_clk()
        if clk:
            out = pd.concat(
                [out, pd.DataFrame([{"Function": func, "Role": "clk", "Chosen": clk, "Notes": "hint"}])],
                ignore_index=True,
            )

    if "reset" not in roles_lower:
        rst = pick_rst()
        if rst:
            out=pd.concat(
                [out, pd.DataFrame([{"Function": func, "Role": "reset", "Chosen": rst, "Notes": "hint"}])],
                ignore_index=True,
            )

    return out

def _dedupe_roles(df_roles: pd.DataFrame, cand_by_role: Dict[str, pd.DataFrame]) -> pd.DataFrame:

    role_ids = set(cand_by_role.keys())
    rows = df_roles[df_roles["Role"].isin(role_ids)].copy()

    used: Dict[str, List[str]] = {}
    for role, name in rows[["Role", "Chosen"]].itertuples(index=False):
        used.setdefault(str(name), []).append(str(role))

    collisions = {name: roles for name, roles in used.items() if name and len(roles) > 1}
    if not collisions:
        return df_roles

    def rank(role: str, var: str) -> int:
        df = cand_by_role.get(role, pd.DataFrame())
        if df.empty:
            return math.inf
        try:
            idxs = df.index[df["Variable Name"].astype(str) == str(var)]
            return int(idxs[0]) if len(idxs) else math.inf
        except Exception:
            return math.inf

    chosen_map = {r: n for r, n in rows[["Role", "Chosen"]].itertuples(index=False)}
    taken = set([n for n in chosen_map.values() if n])

    for name,roles in collisions.items():
        winner = min(roles, key=lambda r: rank(r, name))
        losers = [r for r in roles if r != winner]

        for r in losers:
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
            else:
                chosen_map[r] = ""  

    out = df_roles.copy()
    out.loc[out["Role"].isin(chosen_map.keys()), "Chosen"] = out["Role"].map(chosen_map)
    return out

def _module_from_int(xl_pairs: pd.ExcelFile, func: str) -> str:
    try:
        if "Modules" not in xl_pairs.sheet_names:
            return ""
        dfm = pd.read_excel(xl_pairs, sheet_name="Modules")
        cols = {str(c).lower(): c for c in dfm.columns}
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

def run_single_int(
    *,
    int_pairs:str,
    design1_excel:str,
    design_label: str,
    functions:List[str],
    llm_choice: str,
    select_with_llm: bool,
    out_xlsx: str,
) -> bool:
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
            print(f"[SKIP] {design_label}:{func}: unknown function.")
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
        cand_by_role: Dict[str, pd.DataFrame] = {}
        for role in rule["roles"]:
            if role["kind"] != "signal":
                continue
            dfr = _build_candidates_scored(df_metrics, df_pairs, role, thr_by_var)
            cand_by_role[role["id"]] = dfr

        if select_with_llm:
            prompt = _build_llm_prompt(func, rule["roles"], cand_by_role, thr_by_var, df_metrics)
            out_json = _call_llm(prompt, llm_choice) or {}
            df_roles = _roles_json_to_df(func, out_json)
        else:
            df_roles = _greedy_pick(func, rule["roles"], cand_by_role)

        if df_roles.empty:
            print(f"[SKIP] {design_label}:{func}: no selections produced.")
            continue
        df_roles = _append_clk_reset_hints(df_roles, df_metrics, func)
        df_roles = _dedupe_roles(df_roles, cand_by_role)

        dut_name = _module_from_int(xl_pairs, func)
        if dut_name:
            df_roles = pd.concat(
                [
                    df_roles,
                    pd.DataFrame(
                        [
                            {
                                "Function":func,
                                "Role": "dut",
                                "Chosen": dut_name,
                                "Notes": "from INT.Modules (Design1)",
                            }
                        ]
                    ),
                ],
                ignore_index=True,
            )

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
    ap = argparse.ArgumentParser(description="AES pair-pruning → per-template role selection")

    ap.add_argument("--int_pairs",help="INT workbook from pairwise step (single run)")
    ap.add_argument("--design1_excel", help="Main design metrics (xlsx with AES sheets)")
    ap.add_argument("--design_label", help="Main design label (used for output name)")
    ap.add_argument("--out", help="Output Excel for single run")

    #for batch 
    ap.add_argument("--int_dir",help="Folder containing INT workbooks (batch mode)")
    ap.add_argument("--glob", default="INT_AES_*_aes-T100.xlsx",help="Glob to match INT files in --int_dir")
    ap.add_argument("--design1_excel_dir", default="static_var/AES", help="Where to find per-design metrics in batch")
    ap.add_argument("--out_dir", default="static_mod/AES",help="Where to write final_roles_*.xlsx in batch")
    ap.add_argument(
        "--label_from",
        default="INT_AES_{label}_aes-T100.xlsx",
        help="Pattern to extract {label} from INT filename; must contain '{label}' placeholder",
    )

    ap.add_argument(
        "--functions",
        default="AddRoundKey,SBox,ShiftRows,KeyExpansion",
        help="Comma list of AES function names/sheets to process.",
    )
    ap.add_argument("--llm_choice", default="default")
    ap.add_argument("--no_llm", action="store_true",help="Disable LLM and pick deterministically per role")

    args = ap.parse_args()
    functions = [s.strip() for s in args.functions.split(",") if s.strip()]
    select_with_llm = not args.no_llm

    if args.int_dir:
        int_dir =Path(args.int_dir)
        if not int_dir.is_dir():
            raise FileNotFoundError(f"--int_dir not found: {int_dir}")

        pat = re.escape(args.label_from).replace(r"\{label\}", r"(?P<label>.+)")
        rx = re.compile("^" + pat + "$", re.I)

        count = 0
        for p in sorted(int_dir.glob(args.glob)):
            m = rx.match(p.name)
            if not m:
                print(f"[SKIP] cannot extract label from filename: {p.name}")
                continue
            label = m.group("label")

            metrics1 =Path(args.design1_excel_dir) / f"{label}_output.xlsx"
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
            )
            count += 1 if ok else 0

        if count == 0:
            print(f"[INFO] No INT files matched {args.glob} in {int_dir}")
        return

    if not (args.int_pairs and args.design1_excel):
        raise ValueError("For single run, provide --int_pairs and --design1_excel (or use --int_dir for batch).")

    label = args.design_label or Path(args.design1_excel).stem.replace("_output", "")
    out_xlsx = args.out or f"static_mod/AES/final_roles_{label}.xlsx"

    run_single_int(
        int_pairs=args.int_pairs,
        design1_excel=args.design1_excel,
        design_label=label,
        functions=functions,
        llm_choice=args.llm_choice,
        select_with_llm=select_with_llm,
        out_xlsx=out_xlsx,
    )


if __name__ == "__main__":
    main()



#