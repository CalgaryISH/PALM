
import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

RULES: Dict[str, Dict[str, Any]] = {
    "top_module": {
        "roles": [
            {"id":"clk", "kind": "hint"},
            {"id": "rst", "kind": "hint"},
            {"id": "text_out", "kind": "signal", "dir_any_of":["output"], "width_any_of": [32]},
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
    df.columns =[str(c).strip() for c in df.columns]
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
    xl=pd.ExcelFile(xlsx)
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


def _find_pdg_pack(pdg_json_dir: str, design_label: str, func: str) -> Optional[Path]:
    d = Path(pdg_json_dir)
    if not d.is_dir():
        return None

    candidates = [
        d / f"pdg__{design_label}__{func}.json",
        d / f"pdg__{design_label.replace('_output','')}__{func}.json",
        d / f"pdg__{design_label}_output__{func}.json",
    ]
    for p in candidates:
        if p.exists():
            return p

    hits = sorted(d.glob(f"pdg__*{design_label}*__{func}.json"))
    return hits[0] if hits else None


def _load_pdg_pack(pdg_json_dir: Optional[str], design_label: str, func: str) -> Optional[Dict[str, Any]]:
    if not pdg_json_dir:
        return None
    p = _find_pdg_pack(pdg_json_dir, design_label, func)
    if not p:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _extract_nodes_and_reverse(pack: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, List[str]]]:
    nodes = pack.get("nodes", None)
    rev = pack.get("reverse_edges", None)

    if nodes is None and "pdg" in pack and isinstance(pack["pdg"], dict):
        nodes = pack["pdg"]

    if nodes is None or not isinstance(nodes, dict):
        return {}, {}

    if rev is None or not isinstance(rev, dict):
        rev_built: Dict[str, List[str]] = {k: [] for k in nodes.keys()}
        for tgt, nd in nodes.items():
            conns = []
            if isinstance(nd, dict):
                conns = nd.get("connections", []) or []
            for src in conns:
                s = str(src)
                if s not in rev_built:
                    rev_built[s] = []
                rev_built[s].append(str(tgt))
        for k in list(rev_built.keys()):
            rev_built[k] = sorted(set(rev_built[k]))
        return nodes, rev_built

    rev2: Dict[str, List[str]] = {}
    for k, v in rev.items():
        if isinstance(v, list):
            rev2[str(k)] = [str(x) for x in v]
        else:
            rev2[str(k)] = []
    return nodes, rev2


def _fanin_fanout(pack: Optional[Dict[str, Any]], var: str) -> Tuple[int, int]:
    if not pack or not var:
        return 0, 0

    nodes, rev = _extract_nodes_and_reverse(pack)
    if var not in nodes and var not in rev:
        return 0, 0

    conns = []
    if var in nodes  and isinstance(nodes[var], dict):
        conns = nodes[var].get("connections", []) or []

    fanin_set = {str(x) for x in conns if x and not _is_synth_node(str(x))}
    fanout_set = {str(x) for x in (rev.get(var, []) or []) if x and not _is_synth_node(str(x))}
    return len(fanin_set), len(fanout_set)


def _fan_score_from_role(role_rule: Dict[str, Any], fanin: int, fanout: int) -> float:
    need_dirs = [d.lower() for d in role_rule.get("dir_any_of", [])]
    wants_input = "input" in need_dirs
    wants_output = "output" in need_dirs

    if wants_output and not wants_input:
        return float(fanin - fanout)
    if wants_input and not wants_output:
        return float(fanout - fanin)
    return float(fanin + fanout)

def _module_from_int(xl_pairs: pd.ExcelFile, func: str) -> str:
    try:
        if "Modules" not in xl_pairs.sheet_names:
            return ""
        dfm=pd.read_excel(xl_pairs, sheet_name="Modules")
        cols={c.lower(): c for c in dfm.columns}
        fcol=cols.get("function")
        d1col = cols.get("design1_module") or cols.get("design_1_module") or cols.get("d1_module")
        if not fcol or not d1col:
            return ""
        m = dfm[dfm[fcol].astype(str).str.strip().str.lower() == func.strip().lower()]
        if m.empty:
            return ""
        return str(m.iloc[0][d1col]).strip()
    except Exception:
        return ""

def _greedy_pick(func: str,
                 role_defs: List[Dict[str, Any]],
                 cand_by_role: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for r in role_defs:
        if r["kind"] != "signal":
            continue
        rid = r["id"]
        dfr = cand_by_role.get(rid, pd.DataFrame())

        choice = ""
        note = "no candidates"
        feats = {"FanIn": "", "FanOut": "", "FanScore": ""}

        if not dfr.empty:
            choice = str(dfr.iloc[0]["Variable Name"])
            note = f"min Thr={float(dfr.iloc[0]['Thr']):.6g}"
            if "FanScore" in dfr.columns and not pd.isna(dfr.iloc[0].get("FanScore", None)):
                note += f", FanScore={float(dfr.iloc[0]['FanScore']):.3g}"

            feats = {
                "FanIn": dfr.iloc[0].get("FanIn", ""),
                "FanOut": dfr.iloc[0].get("FanOut", ""),
                "FanScore": dfr.iloc[0].get("FanScore", ""),
            }

        rows.append({"Function": func, "Role": rid,"Chosen": choice,"Notes": note, **feats})

    return pd.DataFrame(rows)

def run_single_int(*,
                   int_pairs: str,
                   design1_excel: str,
                   design_label: str,
                   functions: List[str],
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
            print(f"[SKIP]  {design_label}:{func}: unknown function.")
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

        pack = _load_pdg_pack(pdg_json_dir, design_label, func)

        cand_by_role: Dict[str, pd.DataFrame] = {}
        for role in rule["roles"]:
            if role["kind"] != "signal":
                continue

            dfr = _filter_candidates(df_metrics, df_pairs, role)
            if not dfr.empty:
                dfr = dfr.copy()
                dfr["Thr"] = dfr["Variable Name"].map(thr_by_var).fillna(1e9)
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
                sort_cols = ["Thr"]
                asc = [True]

                if "FanScore" in dfr.columns:
                    sort_cols.append("FanScore")
                    asc.append(False)

                if "PDG_Depth" in dfr.columns:
                    sort_cols.append("PDG_Depth")
                    asc.append(False)

                dfr = dfr.sort_values(sort_cols, ascending=asc, na_position="last")

            cand_by_role[role["id"]] = dfr

        df_roles = _greedy_pick(func, rule["roles"], cand_by_role)
        if df_roles.empty:
            print(f"[SKIP] {design_label}:{func}: no selections produced.")
            continue

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
                }])
            ], ignore_index=True)

        names_all = df_metrics["Variable Name"].astype(str).tolist()
        clk_hint = next((n for n in names_all if re.search(r"\bclk\b", n, re.I)), "")
        rst_hint = next((n for n in names_all if re.search(r"\brst|reset\b", n, re.I)), "")

        if clk_hint:
            df_roles = pd.concat([df_roles, pd.DataFrame([{
                "Function": func, "Role": "clk", "Chosen": clk_hint, "Notes": "hint", "FanIn": "", "FanOut": "", "FanScore": ""
            }])], ignore_index=True)

        if rst_hint:
            df_roles = pd.concat([df_roles, pd.DataFrame([{
                "Function": func, "Role": "rst", "Chosen": rst_hint, "Notes": "hint", "FanIn": "", "FanOut": "", "FanScore": ""
            }])], ignore_index=True)

        all_sheets[func] =df_roles

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

    print(f"{design_label}: wrote final roles → {out_xlsx}")
    return True

def main():
    ap = argparse.ArgumentParser(description="SHA pair-pruning → per-property role selection (with optional FanScore)")

    ap.add_argument("--int_pairs",help="INT workbook from pairwise step (single run)")
    ap.add_argument("--design1_excel", help="Main design metrics (xlsx with SHA sheets)")
    ap.add_argument("--design_label",help="Main design label (used for output name)")
    ap.add_argument("--out", help="Output Excel for single run")
    ap.add_argument("--int_dir",help="Folder containing INT workbooks (batch mode)")
    ap.add_argument("--glob", default=None, help="Glob to match INT files (default: INT_SHA_*_{assistant_name}.xlsx)")
    ap.add_argument("--assistant_name", default="assis_sha", help="Assistant design name")
    ap.add_argument("--design1_excel_dir", default="static_var/SHA", help="Where to find per-design metrics in batch")
    ap.add_argument("--out_dir",default="static_mod/SHA", help="Where to write final_roles_*.xlsx in batch")
    ap.add_argument("--functions",default="top_module",
                    help="Comma list of function/sheet names to process (default: top_module).")

    ap.add_argument("--no_llm", action="store_true", help="Kept for compatibility (SHA script uses greedy selection).")

    ap.add_argument("--pdg_json_dir",default=None,
                    help="Optional folder containing pdg__<design_label>__<func>.json PDG packs")

    args = ap.parse_args()
    functions = [s.strip() for s in args.functions.split(",") if s.strip()]

    if not args.glob:
        args.glob = f"INT_SHA_*_{args.assistant_name}.xlsx"

    label_pattern = f"INT_SHA_{{label}}_{args.assistant_name}.xlsx"

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

            label =m.group("label")

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
                    out_xlsx=str(out_xlsx),
                    pdg_json_dir=args.pdg_json_dir,
            )
            count += 1 if ok else 0

        if count == 0:
            print(f"[INFO] No INT files matched {args.glob} in {int_dir}")
        return

    if not (args.int_pairs and args.design1_excel):
        raise ValueError("For single run, provide --int_pairs and --design1_excel (or use --int_dir for batch).")

    label =args.design_label or Path(args.design1_excel).stem.replace("_output", "")
    out_xlsx = args.out or f"static_mod/SHA/final_roles_{label}.xlsx"

    run_single_int(
             int_pairs=args.int_pairs,
            design1_excel=args.design1_excel,
            design_label=label,
            functions=functions,
            out_xlsx=out_xlsx,
            pdg_json_dir=args.pdg_json_dir,
        )


if __name__ == "__main__":
    main()

