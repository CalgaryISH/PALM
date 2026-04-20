import argparse
import json
import os
import re
from typing import Dict, Any, List, Tuple, Set, Optional

import numpy as np
import openpyxl  
import pandas as pd


LIST_ADDROUNDKEY = [
    "AddRoundKey", "add_rnd_key", "ARKey", "add_round_key_module", "ARKeyModule", "add_rnd_key_fn",
    "AddRoundKeyTransform", "add_rkey", "ARKeyTransform", "xor_add_key", "Add_RndKey_XOR", "RoundKeyAdder",
    "RoundKeyModule", "RndKeyTransform", "RndKey_XOR", "RoundKeyXOR_Module", "key_xor_rnd", "AddKeyXOR",
    "KeyXORAdder", "KeyXORModule", "AddRndXORKey", "AES_AddRndKey", "AddRoundKeyCore", "KeyXORCore", "AES_KeyXOR",
    "AES_RoundXOR", "Add_Rnd_Key_AES", "RoundKey_XOR", "RndKeyAdder", "key_add_xor", "AESKeyAddModule",
]
LIST_SBOX = [
    "sbox", "S_box", "SBOX", "s_box_module", "sbox_unit", "sbox_8bit", "AES_sbox", "aes_S_box", "substitution_box",
    "aes_sub_box", "sub_box", "sbox_logic", "sbox_lookup", "sbox_lut", "sbox_core", "aes_sbox_module",
]
LIST_SHIFTROWS = [
    "shift_rows", "shiftrows", "ShiftRows", "SHIFTROWS", "shift_rows_func", "shift_rows_module", "Shift_Rows",
    "sr_module", "ShiftRowsTransform", "ShiftRowsMatrix", "ShiftRowsFSM",
]

LIST_KEYEXPANSION = [
    "KEYEXPANSION", "key_expansion", "KeyExpansion", "Key_Expander", "KeyScheduler", "KeySched_Mod",
    "AES_Key_Expansion", "RoundKey_Expander", "RoundKeyGenerator", "KeyExpansionCore", "AES_KeySched",
]


LIST_RSA_TOP = [
    "rsa_assistant", "rsa_top", "rsa_core", "rsa_engine", "rsa_main", "rsa_unit", "rsa_top_module",
    "rsa_encrypt", "rsa_decrypt", "rsa_pubexp", "rsa_wrapper", "rsa_controller",
    "modexp_top", "modexp_core", "montgomery_core", "montgomery_top", "rsa_modexp",
    "rsa_datapath", "rsa_compute",
    "modmult", "mulmod", "mulmod_onecycle", "mulmod_twostep"
]

LIST_SHA_TOP = [
    "sha", "sha_top", "sha_core", "sha_engine", "sha_wrapper", "sha_unit",
    "sha1", "sha1_core", "sha256", "sha256_core", "sha224", "sha512", "sha512_core",
    "hash", "hash_core", "digest", "compress", "compression", "message_schedule",
    "top", "top_module", "top_main"
]

LIST_FSM_TOP = [
    "fsm", "fsm_module", "fsm_top", "fsm_core", "state_machine", "state_machine_core",
    "ctrl_fsm", "control_fsm", "controller_fsm", "fsm_ctrl", "state_ctrl", "state_ctrl_fsm",
    "controller", "control", "ctrl", "sequencer", "sm", "main_fsm", "top_fsm",
    "next_state_logic", "state_transition", "transition_logic",
    "arb_fsm", "arbiter_fsm", "dispatch_fsm",
]

IP_REGISTRY: Dict[str, Dict[str, Dict[str, List[str]]]] = {
    "AES": {
        "functions": {
            "AddRoundKey": LIST_ADDROUNDKEY,
            "SBox": LIST_SBOX,
            "ShiftRows": LIST_SHIFTROWS,
            "KeyExpansion": LIST_KEYEXPANSION,
        }
    },
    "RSA": {
        "functions": {
            "top_module": LIST_RSA_TOP,
        }
    },
    "SHA": {
        "functions": {
            "top_module": LIST_SHA_TOP,
        }
    },
    "FSM": {
        "functions": {
            "fsm_module": LIST_FSM_TOP,
        }
    },
}

ASSISTANT_NAMES = {
    "AES": "aes-T100",
    "RSA": "assis_rsa",
    "SHA": "assis_sha",
    "FSM": "assis_fsm",
}













def sdi_name_sim(str1: str, str2: str) -> float:
    s1 = str(str1)
    s2 = str(str2)
    if not s1 or not s2:
        return 0.0
    b1 = set(s1[i:i + 2] for i in range(max(0, len(s1) - 1)))
    b2 = set(s2[i:i + 2] for i in range(max(0, len(s2) - 1)))
    tot = len(b1) + len(b2)
    return 0.0 if tot == 0 else 2 * len(b1 & b2) / tot


def _norm_name(x: Any) -> str:
    s = str(x or "").strip()
    if not s:
        return ""
    if " " in s:
        s = s.split()[-1]
    if "::" in s:
        s = s.split("::")[-1]
    if "." in s:
        s = s.split(".")[-1]
    return s.strip()
def _norm_port_dir(d: Any) -> str:
    if d is None:
        return ""
    if isinstance(d, dict):
        for k in ("kind", "direction", "dir", "name", "value"):
            if k in d:
                dd = _norm_port_dir(d.get(k))
                if dd:
                    return dd
        return ""

    s = str(d).strip().lower()
    if s in {"in", "input", "portdirection.in", "dir_in"}:
        return "input"
    if s in {"out", "output", "portdirection.out", "dir_out"}:
        return "output"
    if s in {"inout", "portdirection.inout", "dir_inout"}:
        return "inout"
    return ""


def build_modules_dict(node: Any, modules_dict: Dict[str, Any]) -> None:
    if isinstance(node, dict):
        if node.get("kind") == "InstanceBody":
            module_name = node.get("name")
            if module_name:
                modules_dict[module_name] = node
            for member in node.get("members", []):
                build_modules_dict(member, modules_dict)
        else:
            for _, value in node.items():
                if isinstance(value, (dict, list)):
                    build_modules_dict(value, modules_dict)
    elif isinstance(node, list):
        for item in node:
            build_modules_dict(item, modules_dict)


def find_best_matching_module(modules_dict: Dict[str, Any], alias_list: List[str]) -> Tuple[str, Any]:
    best_name = None
    best_score = -1.0
    for alias in alias_list:
        al = alias.lower()
        for mod_name in modules_dict.keys():
            sc = sdi_name_sim(al, str(mod_name).lower())
            if sc > best_score:
                best_score = sc
                best_name = mod_name
    return best_name or "", modules_dict.get(best_name)
def _extract_names_from_expr(expr: Any) -> Set[str]:
    out: Set[str] = set()

    def rec(n: Any) -> None:
        if isinstance(n, dict):
            k = n.get("kind", "")
            if k == "NamedValue":
                sym = n.get("symbol", "")
                nm = _norm_name(sym)
                if nm:
                    out.add(nm)
            elif k == "MemberAccess":
                rec(n.get("parent", {}))
            else:
                for _, v in n.items():
                    if isinstance(v, (dict, list)):
                        rec(v)
        elif isinstance(n, list):
            for it in n:
                rec(it)

    rec(expr)
    return out


def _extract_target_from_lhs(lhs: Any) -> str:
    if not isinstance(lhs, dict):
        return ""
    k = lhs.get("kind", "")
    if k == "NamedValue":
        return _norm_name(lhs.get("symbol", ""))
    if k in ("ElementSelect", "RangeSelect"):
        return _extract_target_from_lhs(lhs.get("value", {}))
    if k == "MemberAccess":
        return _extract_target_from_lhs(lhs.get("parent", {}))
    return ""


def extract_signal_stats(module_node: Dict[str, Any], *, family: str) -> Dict[str, Dict[str, Any]]:
    stats: Dict[str, Dict[str, Any]] = {}

    def init_signal(name: str, direction: str = "") -> None:
        nm = _norm_name(name)
        if not nm:
            return
        if nm not in stats:
            t = _norm_port_dir(direction) or (str(direction).lower() if direction else "logic")
            stats[nm] = {
                "type": t,
                "blocking": 0, "nonblocking": 0, "total": 0,
                "if_conditions": 0, "case_conditions": 0, "always_conditions": 0,
                "_is_case_selector": False,
            }

    def bump(name: str, field: str, inc_total: bool = False) -> None:
        nm = _norm_name(name)
        if not nm:
            return
        if nm not in stats:
            init_signal(nm, "logic")
        stats[nm][field] += 1
        if inc_total:
            stats[nm]["total"] += 1

    def handle_case(case_node: Dict[str, Any]) -> None:
        expr = case_node.get("expr", {})
        names = _extract_names_from_expr(expr)
        for nm in names:
            init_signal(nm, "logic")
            bump(nm, "case_conditions", inc_total=False)
            stats[nm]["_is_case_selector"] = True

    def visit(n: Any) -> None:
        if isinstance(n, dict):
            kind = n.get("kind", "")

            if kind in ("Port", "Variable", "Net"):
                nm = _norm_name(n.get("name", ""))
                direction = n.get("direction", "") or n.get("dir", "") or n.get("portDirection", "")
                if nm:
                    init_signal(nm, direction)

            elif kind == "Assignment":
                left = n.get("left", {})
                target = _extract_target_from_lhs(left)
                if target:
                    init_signal(target, "logic")
                    a_type = "nonblocking" if bool(n.get("isNonBlocking", False)) else "blocking"

                    if family != "FSM" and stats.get(target, {}).get("_is_case_selector", False):
                        pass
                    else:
                        bump(target, a_type, inc_total=True)

            elif kind == "SignalEvent":
                expr = n.get("expr", {})
                for nm in _extract_names_from_expr(expr):
                    init_signal(nm, "logic")
                    bump(nm, "always_conditions", inc_total=False)

            elif kind == "Conditional":
                for cond in n.get("conditions", []):
                    expr = cond.get("expr", {})
                    for nm in _extract_names_from_expr(expr):
                        init_signal(nm, "logic")
                        bump(nm, "if_conditions", inc_total=False)

            elif kind == "Case":
                handle_case(n)

            for _, v in n.items():
                if isinstance(v, (dict, list)):
                    visit(v)

        elif isinstance(n, list):
            for it in n:
                visit(it)

    visit(module_node)

    for d in stats.values():
        d.pop("_is_case_selector", None)

    return stats


#for Threshold:::: Thr=(1-SDI)*10+statd*0.0001+geom*0.1 +width_penalty
def calculate_pairwise(df1: pd.DataFrame, df2: pd.DataFrame) -> pd.DataFrame:
    need_metrics = ["PDG_Depth", "Num_Operators", "Centroid", "Bit Width"]
    stat_feats = ["blocking", "nonblocking", "total", "if_conditions", "case_conditions", "always_conditions"]

    for cols in (need_metrics, stat_feats):
        for c in cols:
            if c in df1.columns:
                df1[c] = pd.to_numeric(df1[c], errors="coerce").fillna(0)
            if c in df2.columns:
                df2[c] = pd.to_numeric(df2[c], errors="coerce").fillna(0)

    pairs = []
    geom = []
    statd = []
    sdis = []
    width_pen = []

    for _, r1 in df1.iterrows():
        v1 = str(r1.get("Variable Name", "") or "")
        g1 = np.array([r1.get("PDG_Depth", 0), r1.get("Num_Operators", 0), r1.get("Centroid", 0)], dtype=float)
        s1 = np.array([r1.get(f, 0) for f in stat_feats], dtype=float)
        w1 = float(r1.get("Bit Width", 0) or 0)

        for _, r2 in df2.iterrows():
                v2 = str(r2.get("Variable Name", "") or "")
                g2 = np.array([r2.get("PDG_Depth", 0), r2.get("Num_Operators", 0), r2.get("Centroid", 0)], dtype=float)
                s2 = np.array([r2.get(f, 0) for f in stat_feats], dtype=float)
                w2 = float(r2.get("Bit Width", 0) or 0)

                d_geom = float(np.linalg.norm(g1 - g2))
                d_stat = float(np.linalg.norm(s1 - s2))
                sdi = float(sdi_name_sim(v1, v2))
                wp = 0.0
                if w1 > 0 and w2 > 0 and w1 != w2:
                    wp = 2.5

                pairs.append(f"{v1} - {v2}")
                geom.append(d_geom)
                statd.append(d_stat)
                sdis.append(sdi)
                width_pen.append(wp)

    geom = np.array(geom, dtype=float)
    statd = np.array(statd, dtype=float)
    sdis = np.array(sdis, dtype=float)
    width_pen = np.array(width_pen, dtype=float)

    thr = ((1.0 - sdis) * 10.0) + (statd * 0.0001) + (geom * 0.1) + width_pen

    return pd.DataFrame({
            "Variable Pair":pairs,
            "Euclidean Distance":geom,
            "Stat_euclidean_distance": statd,
            "Seman_sorensen_dice_coefficient": sdis,
            "WidthPenalty": width_pen,
            "Thr": thr,
    })

def run_single(
        family: str,
        design1_json: str,
        design2_json: str,
        design1_excel: str,
        design2_excel: str,
        design1_label: str,
        design2_label: str,
        out_file: str
) -> None:

    reg = IP_REGISTRY[family]
    functions = reg["functions"]

    with open(design1_json, "r", encoding="utf-8") as f1:
        ast1 = json.load(f1)
    with open(design2_json, "r", encoding="utf-8") as f2:
        ast2 = json.load(f2)

    mods1: Dict[str, Any] = {}
    mods2: Dict[str, Any] = {}
    build_modules_dict(ast1, mods1)
    build_modules_dict(ast2, mods2)

    excel1 = pd.ExcelFile(design1_excel)
    excel2 = pd.ExcelFile(design2_excel)

    output_pairs: Dict[str, pd.DataFrame] = {}
    modules_rows: List[Tuple[str, str, str]] = []

    print(f"\n=== Family={family} | D1={design1_label} | D2={design2_label} ===")

    for func_name, alias_list in functions.items():
        print(f"\n--- Function={func_name} ---")

        best1_name, best1_node = find_best_matching_module(mods1, alias_list)
        best2_name, best2_node = find_best_matching_module(mods2, alias_list)

        if best1_node is None or best2_node is None:
            print(f"[WARN] Missing module for '{func_name}' in one/both designs. Skipping.")
            continue

        print(f"Matched modules: {design1_label}:{best1_name}  |  {design2_label}:{best2_name}")
        modules_rows.append((func_name, best1_name, best2_name))
        stats1 = extract_signal_stats(best1_node, family=family)
        stats2 = extract_signal_stats(best2_node, family=family)
        df_s1 = pd.DataFrame.from_dict(stats1, orient="index").reset_index().rename(columns={"index": "Variable Name"})
        df_s2 = pd.DataFrame.from_dict(stats2, orient="index").reset_index().rename(columns={"index": "Variable Name"})

        if "type" in df_s1.columns and "Type" not in df_s1.columns:
            df_s1 = df_s1.rename(columns={"type": "Type"})
        if "type" in df_s2.columns and "Type" not in df_s2.columns:
            df_s2 = df_s2.rename(columns={"type": "Type"})

        if func_name not in excel1.sheet_names or func_name not in excel2.sheet_names:
            print(f"[WARN] Sheet '{func_name}' missing in one/both workbooks. Skipping pairwise for this function.")
            continue

        df_m1 = pd.read_excel(excel1, sheet_name=func_name)

        df_m2 = pd.read_excel(excel2, sheet_name=func_name)

        df_m1_en = pd.merge(df_m1, df_s1, on="Variable Name", how="left", suffixes=("", "_stat"))
        df_m2_en = pd.merge(df_m2, df_s2, on="Variable Name", how="left", suffixes=("", "_stat"))

        std_cols = [
            "Variable Name", "Type", "Bit Width",
            "PDG_Depth", "Num_Operators", "Centroid",
            "blocking", "nonblocking", "total",
            "if_conditions", "case_conditions", "always_conditions"
        ]
        for col in std_cols:
            if col not in df_m1_en.columns:
                df_m1_en[col] = pd.NA
            if col not in df_m2_en.columns:
                df_m2_en[col] = pd.NA

        for c in ["Bit Width", "PDG_Depth", "Num_Operators", "Centroid",
                  "blocking", "nonblocking", "total",
                  "if_conditions", "case_conditions", "always_conditions"]:
            if c in df_m1_en.columns:
                df_m1_en[c] = pd.to_numeric(df_m1_en[c], errors="coerce").fillna(0)
            if c in df_m2_en.columns:
                df_m2_en[c] = pd.to_numeric(df_m2_en[c], errors="coerce").fillna(0)

        df_pairs = calculate_pairwise(df_m1_en[std_cols].copy(), df_m2_en[std_cols].copy())
        output_pairs[func_name] = df_pairs

    if not output_pairs:
        print("[INFO]:::::::: no function produced output")
        return

    os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
    with pd.ExcelWriter(out_file, engine="openpyxl") as writer:
        for func, df in output_pairs.items():
            df.to_excel(writer, sheet_name=func[:31], index=False)

        if modules_rows:
            pd.DataFrame(modules_rows, columns=["Function", "Design1_Module", "Design2_Module"]) \
                .to_excel(writer, sheet_name="Modules", index=False)

    print(f"[OK] Results saved to {out_file}")


def main():
    ap = argparse.ArgumentParser(description="Multi-IP family analysis pipeline (AES/RSA/SHA/FSM)")
    ap.add_argument("--family",required=True, choices=list(IP_REGISTRY.keys()), help="IP family to use")
    ap.add_argument("--design1_json", required=True, help="Path to design 1 jsn AST or a dir for batch")
    ap.add_argument("--design2_json", required=True, help="Path to fixed assistant json")
    ap.add_argument("--design1_excel",help="Path to design 1 Excel metrics (single run)")
    ap.add_argument("--design2_excel",required=True,help="path to assistant Excel metrics")
    ap.add_argument("--design1_label", help="label for design 1 (single run)")
    ap.add_argument("--design2_label", default=None,help="Label for design 2")
    ap.add_argument("--out", help="Output Excel filename (single run)")
    ap.add_argument("--out_dir",help="where to place INT workbooks in batch mode")
    ap.add_argument("--design1_excel_dir", help="folder to look up design1 Excel in batch mode")

    args = ap.parse_args()
    if not args.design2_label:
        args.design2_label = ASSISTANT_NAMES.get(args.family, f"{args.family.lower()}-assistant")
    if not args.out_dir:
        args.out_dir = f"static_pairs/{args.family}"

    if not args.design1_excel_dir:
        args.design1_excel_dir = f"static_var/{args.family}"

    #for batch mode
    if os.path.isdir(args.design1_json):
        d1_json_dir = args.design1_json
        os.makedirs(args.out_dir, exist_ok=True)

        if not os.path.isfile(args.design2_json):
            raise FileNotFoundError(f"--design2_json not found: {args.design2_json}")
        if not os.path.isfile(args.design2_excel):
            raise FileNotFoundError(f"--design2_excel not found: {args.design2_excel}")

        json_files = [f for f in os.listdir(d1_json_dir) if f.lower().endswith(".json")]

        skip_name = args.design2_label.lower()
        json_files = [f for f in json_files if skip_name not in os.path.splitext(f)[0].lower()]

        if not json_files:
            print(f"no json files found in {d1_json_dir}")
            return

        for jf in sorted(json_files):
            d1_json_path = os.path.join(d1_json_dir, jf)
            base = os.path.splitext(jf)[0]
            label = base[:-7] if base.endswith("_output") else base
            d1_xlsx_path = os.path.join(args.design1_excel_dir, base + ".xlsx")
            if not os.path.isfile(d1_xlsx_path):
                print(f"[WARN] Missing design1 Excel for {jf}: {d1_xlsx_path} (skipping)")
                continue
            out_file = os.path.join(args.out_dir, f"INT_{args.family}_{label}_{args.design2_label}.xlsx")
            try:
                run_single(
                        family=args.family,
                        design1_json=d1_json_path,
                        design2_json=args.design2_json,
                        design1_excel=d1_xlsx_path,
                        design2_excel=args.design2_excel,
                        design1_label=label,
                        design2_label=args.design2_label,
                        out_file=out_file
                )
            except Exception as e:
                print(f"[ERROR] Failed on {jf}: {e}")

        return
    #for single 
    if not args.design1_excel:
        raise ValueError("--design1_excel is required for single run when --design1_json is a a file")

    if not args.design1_label:
        base = os.path.splitext(os.path.basename(args.design1_json))[0]
        args.design1_label = base[:-7] if base.endswith("_output") else base

    out_file = args.out or f"static_pairs/{args.family}/INT_{args.family}_{args.design1_label}_{args.design2_label}.xlsx"

    run_single(
        family=args.family,
            design1_json=args.design1_json,
            design2_json=args.design2_json,
            design1_excel=args.design1_excel,
            design2_excel=args.design2_excel,
            design1_label=args.design1_label,
            design2_label=args.design2_label,
            out_file=out_file
    )


if __name__ == "__main__":

    main()