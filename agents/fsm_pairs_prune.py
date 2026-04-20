import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


FUNC_DEFAULT = "fsm_module"
SHEET_ALWAYS = "always_legal_state"
SHEET_RECOV = "recovery_from_illegal_state"


def _try_load_json(p: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def _norm_name(x: Any) -> str:
    s = str(x or "").strip()
    if not s:
        return ""
    if " " in s:
        s = s.split()[-1]
    # strip hierarchy
    if "::" in s:
        s = s.split("::")[-1]
    if "." in s:
        s = s.split(".")[-1]
    return s


def _is_synth_node(name: str) -> bool:
    s = str(name)
    return s.startswith("cond_") or s.startswith("case_")


def _extract_nodes_map(pack: Dict[str, Any]) -> Dict[str, Any]:
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
        out: Dict[str, List[str]] = {}
        for k, v in rev.items():
            out[str(k)] = [str(x) for x in v] if isinstance(v, list) else []
        return out

    # build if missing
    rb: Dict[str, List[str]] = {}
    for tgt, nd in nodes.items():
        if not isinstance(nd, dict):
            continue
        for src in (nd.get("connections") or []):
            rb.setdefault(str(src), []).append(str(tgt))
    for k in list(rb.keys()):
        rb[k] = sorted(set(rb[k]))
    return rb



def _label_variants(*, design_label: str, design1_excel: str, int_pairs: str) -> List[str]:
    cands: set[str] = set()

    def add(x: str) -> None:
        x = (x or "").strip()
        if x:
            cands.add(x)

    add(design_label)
    add(Path(design1_excel).stem.replace("final_roles_", "").replace("_output", ""))

    #INT_FSM_<labl>_assis_fsm.xlsx
    m = re.search(r"INT_FSM_(?P<label>.+?)_", Path(int_pairs).name, flags=re.I)
    if m:
        add(m.group("label"))
    for x in list(cands):
            add(x.replace("_", ""))
            add(re.sub(r"([a-zA-Z]+)(\d+)$", r"\1_\2", x))
            add(f"{x}_output")
            add(f"{x.replace('_','')}_output")

    return sorted(set(cands))


def _best_pack_from_candidates(
    cands: List[Path], *, func: str, labels: List[str], expect_design_file: str
) -> Optional[Path]:
    if not cands:
        return None

    func_l = func.lower()

    for p in cands:
        pack = _try_load_json(p)
        if not isinstance(pack, dict):
            continue
        if str(pack.get("function", "")).strip().lower() != func_l:
            continue
        if str(pack.get("design_file", "")).strip().lower() == expect_design_file.lower():
            return p

    for p in cands:
        s = p.name.lower()
        if not s.endswith(f"__{func_l}.json"):
            continue
        if any(lab.lower() in s for lab in labels if lab):
            return p

    for p in cands:
        if p.name.lower().endswith(f"__{func_l}.json"):
            return p

    return cands[0]


def load_pdg_pack(
    pdg_json_dir: Optional[str],
    *,
    design_label: str,
    func: str,
    design1_excel: str,
    int_pairs: str,
    debug: bool = True,
) -> Optional[Dict[str, Any]]:
    labels = _label_variants(design_label=design_label, design1_excel=design1_excel, int_pairs=int_pairs)
    expect_design_file = f"{Path(design1_excel).stem.replace('_output','')}.json"

    if pdg_json_dir:
        p = Path(pdg_json_dir)

        if not p.exists() and p.parent.exists():
            name = p.name
            if name.startswith("pdg_") and not name.startswith("pdg__"):
                alt = p.parent / ("pdg__" + name[len("pdg_"):])
                if alt.exists():
                    p = alt
            elif name.startswith("pdg__"):
                alt = p.parent / ("pdg_" + name[len("pdg__"):])
                if alt.exists():
                    p = alt

        if p.is_file() and p.suffix.lower() == ".json":
            pack = _try_load_json(p)
            if isinstance(pack, dict):
                if debug:
                    print(f"[PDG] loaded pack file: {p}")
                return pack

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
                    cand = d / pat
                    if cand.exists():
                        pack = _try_load_json(cand)
                        if isinstance(pack, dict):
                            if debug:
                                print(f"[PDG] loaded: {cand}")
                            return pack

            globs: List[Path] = []
            for lab in labels:
                globs += list(d.glob(f"pdg__*{lab}*__{func}.json"))
            globs = sorted(set(globs))
            pick = _best_pack_from_candidates(globs, func=func, labels=labels, expect_design_file=expect_design_file)
            if pick:
                pack = _try_load_json(pick)
                if isinstance(pack, dict):
                    if debug:
                        print(f"[PDG] glob-loaded: {pick}")
                    return pack
        else:
            if debug:
                print(f"[PDG] --pdg_json_dir is not a directory/file: {pdg_json_dir}")

    root = Path(".")
    candidates = sorted(root.rglob(f"pdg__*__{func}.json"))
    if debug:
        print(f"[PDG] auto-find: {len(candidates)} candidate packs for func={func}")

    pick = _best_pack_from_candidates(candidates, func=func, labels=labels, expect_design_file=expect_design_file)
    if not pick:
        return None
    pack = _try_load_json(pick)
    if isinstance(pack, dict):
        if debug:
            print(f"[PDG] auto-find loaded: {pick} (design_file={pack.get('design_file')})")
        return pack
    return None


def _read_metrics(xlsx: str, sheet: str) -> pd.DataFrame:
    xl = pd.ExcelFile(xlsx)
    use_sheet = sheet if sheet in xl.sheet_names else xl.sheet_names[0]
    df = pd.read_excel(xlsx, sheet_name=use_sheet)
    df.columns = [str(c).strip() for c in df.columns]
    if "Variable Name" not in df.columns:
        for alt in ("Signal", "Name", "Var"):
            if alt in df.columns:
                df = df.rename(columns={alt: "Variable Name"})
                break
    if "Type" not in df.columns:
        df["Type"] = ""
    if "Bit Width" in df.columns:
        df["Bit Width"] = pd.to_numeric(df["Bit Width"], errors="coerce")
    return df


def _pick_clk_reset_from_metrics(df: pd.DataFrame) -> Tuple[List[str], List[str]]:
    names = df.get("Variable Name", pd.Series([], dtype=str)).astype(str).tolist()
    types = df.get("Type", pd.Series([], dtype=str)).astype(str).tolist()

    inputs = [n for n, t in zip(names, types) if "input" in t.lower()]

    clk_cands = [n for n in inputs if re.search(r"(^|_)clk($|_)|clock", n, flags=re.I)]
    rst_cands = [n for n in inputs if re.search(r"rst|reset", n, flags=re.I)]

    if not clk_cands and inputs:
        clk_cands = inputs[:]
    if not rst_cands and inputs:
        rst_cands = inputs[:]

    return clk_cands, rst_cands

def _enum_var_candidates(nodes: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for name, nd in nodes.items():
        if not isinstance(nd, dict):
            continue
        if str(nd.get("node_type", "")).lower() != "variable":
            continue

        vt = str(nd.get("var_type", "") or "").strip().lower()
        if vt == "enum":
            out[str(name)] = nd
            continue

        decl_t = str(nd.get("decl_type", "") or nd.get("type", "") or "")
        if "enum{" in decl_t.lower():
            out[str(name)] = nd
            continue
    return out


def _choose_enum_typedef(pack: Dict[str, Any], nodes: Dict[str, Any]) -> Tuple[str, int, List[str]]:
    enums = pack.get("enums") if isinstance(pack.get("enums"), dict) else {}
    if not enums:
        return "", 0, []

    if len(enums) == 1:
        en = next(iter(enums.keys()))
        w = int(enums[en].get("width") or 0)
        mem = [
            str(m.get("name"))
            for m in (enums[en].get("members") or [])
            if isinstance(m, dict) and m.get("name")
        ]
        return str(en), w, mem

    ev = _enum_var_candidates(nodes)

    best_name = ""
    best_score = -1
    best_w = 0
    best_mem: List[str] = []

    for enum_name, ed in enums.items():
        members = [
            str(m.get("name"))
            for m in (ed.get("members") or [])
            if isinstance(m, dict) and m.get("name")
        ]
        score = 0
        for _, nd in ev.items():
            conns = [str(x) for x in (nd.get("connections") or [])]
            if any(m in conns for m in members):
                score += 1

        if score > best_score:
            best_name = str(enum_name)
            best_score = score
            best_w = int(ed.get("width") or 0)
            best_mem = members

    if not best_name:
        enum_name = max(enums.keys(), key=lambda k: len(enums[k].get("members") or []))
        ed = enums[enum_name]
        best_name = str(enum_name)
        best_w = int(ed.get("width") or 0)
        best_mem = [
            str(m.get("name"))
            for m in (ed.get("members") or [])
            if isinstance(m, dict) and m.get("name")
        ]

    return best_name, best_w, best_mem


def _parse_enum_type_string(type_s: str) -> Tuple[str, int, List[str]]:
    s = (type_s or "").strip()
    if "enum{" not in s or "}" not in s:
        return "", 0, []

    try:
        pre, rest = s.split("enum{", 1)
        body, suffix = rest.split("}", 1)
    except ValueError:
        return "", 0, []

    suffix = (suffix or "").strip()
    typedef_name = suffix
    if "." in suffix:
        typedef_name = suffix.split(".")[-1].strip()

    members: List[str] = []
    width = 0
    for item in [x.strip() for x in body.split(",") if x.strip()]:
        if "=" in item:
            n, v = item.split("=", 1)
            name = n.strip()
            val = v.strip()
        else:
            name = item.strip()
            val = ""
        if name:
            members.append(name)
        if width == 0 and val:
            m = re.search(r"(\d+)\s*'\s*[bdhoBDHO]", val)
            if m:
                try:
                    width = int(m.group(1))
                except Exception:
                    width = 0

    return typedef_name, width, members


def _try_load_rtl_ast(pack: Dict[str, Any], *, rtl_json_dir: Optional[str], debug: bool = False) -> Optional[Dict[str, Any]]:
    design_file = str(pack.get("design_file") or "").strip()
    if not design_file:
        return None

    if rtl_json_dir:
        p = Path(rtl_json_dir)
        if p.is_file() and p.suffix.lower() == ".json":
            ast = _try_load_json(p)
            return ast if isinstance(ast, dict) else None
        if p.is_dir():
            cand = p / design_file
            if cand.is_file():
                ast = _try_load_json(cand)
                return ast if isinstance(ast, dict) else None
            # sometimes files are nested by family
            fam = str(pack.get("family") or "").strip()
            if fam:
                cand2 = p / fam / design_file
                if cand2.is_file():
                    ast = _try_load_json(cand2)
                    return ast if isinstance(ast, dict) else None

    rtl_root = Path("rtl_json")
    if rtl_root.exists() and rtl_root.is_dir():
        hits = list(rtl_root.rglob(design_file))
        if hits:
            ast = _try_load_json(hits[0])
            return ast if isinstance(ast, dict) else None

    hits2 = list(Path(".").rglob(design_file))
    if hits2:
        ast = _try_load_json(hits2[0])
        return ast if isinstance(ast, dict) else None

    if debug:
        print(f"[RTL] could not locate main AST JSON for {design_file}")
    return None


def _find_var_decl_type(ast: Dict[str, Any], var_name: str, module_name: Optional[str] = None) -> Optional[str]:
    v = (var_name or "").strip()
    if not v:
        return None

    target_mod = (module_name or "").strip()

    def iter_dicts(obj):
        if isinstance(obj, dict):
            yield obj
            for vv in obj.values():
                yield from iter_dicts(vv)
        elif isinstance(obj, list):
            for it in obj:
                yield from iter_dicts(it)

    if target_mod:
        for d in iter_dicts(ast):
            if d.get("kind") == "InstanceBody" and str(d.get("name", "")) == target_mod:
                # search within this subtree for the Variable
                for dd in iter_dicts(d):
                    if dd.get("kind") == "Variable" and str(dd.get("name", "")) == v:
                        t = dd.get("type")
                        return str(t) if isinstance(t, str) else None
                break

    for d in iter_dicts(ast):
        if d.get("kind") == "Variable" and str(d.get("name", "")) == v:
            t = d.get("type")
            return str(t) if isinstance(t, str) else None
    return None


def _recover_enum_from_main_ast(
    *,
    pack: Dict[str, Any],
    enum_vars: List[str],
    state_var: str,
    rtl_json_dir: Optional[str],
    debug: bool = False,
) -> Tuple[str, int, List[str]]:
    ast = _try_load_rtl_ast(pack, rtl_json_dir=rtl_json_dir, debug=debug)
    if not isinstance(ast, dict):
        return "", 0, []

    module_name = str(pack.get("module") or "").strip() or None

    if state_var:
        t = _find_var_decl_type(ast, state_var, module_name=module_name)
        if isinstance(t, str):
            typedef_name, width, members = _parse_enum_type_string(t)
            if members:
                return typedef_name, width, members

    for v in enum_vars or []:
        t = _find_var_decl_type(ast, v, module_name=module_name)
        if isinstance(t, str):
            typedef_name, width, members = _parse_enum_type_string(t)
            if members:
                return typedef_name, width, members
    def iter_dicts(obj):
        if isinstance(obj, dict):
            yield obj
            for vv in obj.values():
                yield from iter_dicts(vv)
        elif isinstance(obj, list):
            for it in obj:
                yield from iter_dicts(it)

    for d in iter_dicts(ast):
        if d.get("kind") == "Variable" and isinstance(d.get("type"), str):
            t = d["type"]
            if "enum{" in t and "}" in t:
                typedef_name, width, members = _parse_enum_type_string(t)
                if members:
                    return typedef_name, width, members

    return "", 0, []


def _count_synth_fanout(rev: Dict[str, List[str]], var: str) -> Tuple[int, int, int]:
    outs = [str(x) for x in (rev.get(var) or [])]
    case_out = sum(1 for x in outs if x.startswith("case_"))
    cond_out = sum(1 for x in outs if x.startswith("cond_"))
    synth_out = sum(1 for x in outs if _is_synth_node(x))
    return case_out, cond_out, synth_out


def _pick_state_and_next(
    *,
    enum_vars: Dict[str, Dict[str, Any]],
    rev: Dict[str, List[str]],
    enum_members: List[str],
) -> Tuple[str, str]:
    if not enum_vars:
        return "", ""

    scored: List[Tuple[Tuple[int, int, int, int], str]] = []
    for v, nd in enum_vars.items():
        case_out, cond_out, synth_out = _count_synth_fanout(rev, v)
        depth = int(nd.get("depth") or 0)
        score = (case_out, cond_out, synth_out, depth)
        scored.append((score, v))

    scored.sort(key=lambda t: t[0], reverse=True)
    state_var = scored[0][1]

    next_scored: List[Tuple[Tuple[int, int, int], str]] = []
    for v, nd in enum_vars.items():
        if v == state_var:
            continue
        feeds_state = 1 if state_var in (rev.get(v) or []) else 0
        case_out, _, _ = _count_synth_fanout(rev, v)
        depth = int(nd.get("depth") or 0)
        next_scored.append(((feeds_state, case_out, depth), v))

    next_scored.sort(key=lambda t: t[0], reverse=True)
    next_var = next_scored[0][1] if next_scored else ""

    return state_var, next_var


def _backtick(x: str) -> str:
    x = (x or "").strip()
    return f"`{x}`" if x else ""

def _build_sheet_always(
    *,
    clk_cands: List[str],
    rst_cands: List[str],
    enum_typedef: str,
    enum_width: int,
    enum_members: List[str],
    enum_vars: List[str],
    state_var: str,
    state_width: int,
) -> pd.DataFrame:
    legal_list = ", ".join(enum_members)

    rows = [
        {
            "candidate(s)_in_design": ", ".join(clk_cands) if clk_cands else "",
            "chosen": _backtick(clk_cands[0]) if clk_cands else "",
            "role": "clk",
            "width": 1,
        },
        {
            "candidate(s)_in_design": ", ".join(rst_cands) if rst_cands else "",
            "chosen": _backtick(rst_cands[0]) if rst_cands else "",
            "role": "reset_disable",
            "width": 1,
        },
        {
            "candidate(s)_in_design": ", ".join(enum_vars) if enum_vars else "",
            "chosen": _backtick(state_var),
            "role": "state",
            "width": int(state_width or enum_width or 0) or "",
        },
        {
            "candidate(s)_in_design": enum_typedef,
            "chosen": _backtick(enum_typedef),
            "role": "enum_structure_name",
            "width": "",
        },
        {
            "candidate(s)_in_design": "enum_members",
            "chosen": _backtick(legal_list),
            "role": "legal_set",
            "width": int(enum_width or 0) or "",
        },
    ]

    return pd.DataFrame(rows, columns=["candidate(s)_in_design", "chosen", "role", "width"])


def _build_sheet_recovery(
    *,
    clk_cands: List[str],
    rst_cands: List[str],
    enum_typedef: str,
    enum_width: int,
    enum_members: List[str],
    enum_vars: List[str],
    state_var: str,
    state_width: int,
    next_var: str,
    next_width: int,
) -> pd.DataFrame:
    legal_list = ", ".join(enum_members)
    safe_state = enum_members[0] if enum_members else ""

    next_candidates = [v for v in enum_vars if v != state_var]

    rows = [
        {
            "candidate(s)_in_design": ", ".join(clk_cands) if clk_cands else "",
            "chosen": _backtick(clk_cands[0]) if clk_cands else "",
            "role": "clk",
            "width": 1,
        },
        {
            "candidate(s)_in_design": ", ".join(rst_cands) if rst_cands else "",
            "chosen": _backtick(rst_cands[0]) if rst_cands else "",
            "role": "reset_disable",
            "width": 1,
        },
        {
            "candidate(s)_in_design": ", ".join(enum_vars) if enum_vars else "",
            "chosen": _backtick(state_var),
            "role": "state",
            "width": int(state_width or enum_width or 0) or "",
        },
        {
            "candidate(s)_in_design": ", ".join(next_candidates) if next_candidates else "",
            "chosen": _backtick(next_var),
            "role": "next_state",
            "width": int(next_width or enum_width or 0) or "",
        },
        {
            "candidate(s)_in_design": "first_legal_member",
            "chosen": _backtick(safe_state),
            "role": "safe_state",
            "width": int(enum_width or 0) or "",
        },
        {
            "candidate(s)_in_design": "enum_members",
            "chosen": _backtick(legal_list),
            "role": "legal_set",
            "width": int(enum_width or 0) or "",
        },
        {
            "candidate(s)_in_design": enum_typedef,
            "chosen": _backtick(enum_typedef),
            "role": "enum_structure_name",
            "width": "",
        },
    ]

    return pd.DataFrame(rows, columns=["candidate(s)_in_design", "chosen", "role", "width"])



def main() -> None:
    ap = argparse.ArgumentParser(description="FSM pairs prune + role selection ")
    ap.add_argument("--int_pairs", required=True, help="INT workbook path ")
    ap.add_argument("--design1_excel", required=True, help="Design1 metrics workbook (.xlsx)")
    ap.add_argument("--design_label", required=True, help="Label for the design (ex fsm_v1)")
    ap.add_argument("--out", required=True, help="Output final roles workbook path")
    ap.add_argument("--pdg_json_dir", default=None, help="optional") #add help
    ap.add_argument(
        "--rtl_json_dir",
        default=None,
        help=(
            "Optional directory (or file) for the MAIN AST JSON. "
            "Used as fallback to recover enum members when the PDG pack lacks them. "
            "If a directory is given, the script looks for pack['design_file'] inside it. "
            "If omitted, it will try to auto-find pack['design_file'] under ./rtl_json/ and the project tree."
        ),
    )
    ap.add_argument("--func", default=FUNC_DEFAULT, help=f"Function name in PDG pack (default: {FUNC_DEFAULT})")
    args = ap.parse_args()
    pack = load_pdg_pack(
        args.pdg_json_dir,
        design_label=args.design_label,
        func=args.func,
        design1_excel=args.design1_excel,
        int_pairs=args.int_pairs,
        debug=True,
    )
    if not pack:
        raise SystemExit("[ERROR] Could not locate/load PDG pack JSON. Provide --pdg_json_dir.")

    nodes = _extract_nodes_map(pack)
    rev = _extract_reverse_edges(pack, nodes)
    dfm = _read_metrics(args.design1_excel, args.func)

    #clock reset candidates
    inputs = [str(x) for x in (pack.get("inputs") or [])]
    clk_cands = [x for x in inputs if re.search(r"(^|_)clk($|_)|clock", x, flags=re.I)]
    rst_cands = [x for x in inputs if re.search(r"rst|reset", x, flags=re.I)]

    if not clk_cands or not rst_cands:
        mclk, mrst = _pick_clk_reset_from_metrics(dfm)
        if not clk_cands:
            clk_cands = mclk
        if not rst_cands:
            rst_cands = mrst

    if not clk_cands and inputs:
        clk_cands = [inputs[0]]
    if not rst_cands and inputs:
        rst_cands = [inputs[0]]

    enum_typedef, enum_width, enum_members = _choose_enum_typedef(pack, nodes)

    enum_vars_map = _enum_var_candidates(nodes)

    if enum_members:
        filtered: Dict[str, Dict[str, Any]] = {}
        for v, nd in enum_vars_map.items():
            conns = [str(x) for x in (nd.get("connections") or [])]
            if any(m in conns for m in enum_members):
                filtered[v] = nd
        # only apply filter if it doesn't eliminate all
        if filtered:
            enum_vars_map = filtered

    enum_vars_sorted = sorted(enum_vars_map.keys())

    state_var, next_var = _pick_state_and_next(enum_vars=enum_vars_map, rev=rev, enum_members=enum_members)

    # widths
    def _bw(var: str) -> int:
        if not var:
            return 0
        nd = enum_vars_map.get(var)
        if isinstance(nd, dict):
            bw = nd.get("bit_width")
            try:
                return int(bw) if bw is not None else 0
            except Exception:
                return 0
        return 0

    state_w = _bw(state_var) or enum_width
    next_w = _bw(next_var) or enum_width


    if (not enum_members) or (not enum_typedef) or (not enum_width):
        t2, w2, m2 = _recover_enum_from_main_ast(
            pack=pack,
            enum_vars=enum_vars_sorted,
            state_var=state_var or "",
            rtl_json_dir=args.rtl_json_dir,
            debug=True,
        )
        if m2 and not enum_members:
            enum_members = m2
        if t2 and not enum_typedef:
            enum_typedef = t2
        if w2 and not enum_width:
            enum_width = w2
        # refresh printed widths
        state_w = _bw(state_var) or enum_width
        next_w = _bw(next_var) or enum_width

    print("\n[ENUM]")
    print(f"  typedef: {enum_typedef}  width={enum_width}")
    print(f"  members: {enum_members}")
    print(f"  enum vars: {enum_vars_sorted}")
    print("\n[PICKS]")
    print(f"  clk: {clk_cands[:3]}{' ...' if len(clk_cands)>3 else ''}")
    print(f"  reset_disable: {rst_cands[:3]}{' ...' if len(rst_cands)>3 else ''}")
    print(f"  state -> {state_var} (w={state_w})")
    print(f"  next_state -> {next_var} (w={next_w})")

    df_always = _build_sheet_always(
        clk_cands=clk_cands,
        rst_cands=rst_cands,
        enum_typedef=enum_typedef,
        enum_width=enum_width,
        enum_members=enum_members,
        enum_vars=enum_vars_sorted,
        state_var=state_var,
        state_width=state_w,
    )

    df_recov = _build_sheet_recovery(
        clk_cands=clk_cands,
        rst_cands=rst_cands,
        enum_typedef=enum_typedef,
        enum_width=enum_width,
        enum_members=enum_members,
        enum_vars=enum_vars_sorted,
        state_var=state_var,
        state_width=state_w,
        next_var=next_var,
        next_width=next_w,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp.xlsx")

    with pd.ExcelWriter(tmp, engine="openpyxl") as w:
        df_always.to_excel(w, sheet_name=SHEET_ALWAYS[:31], index=False)
        df_recov.to_excel(w, sheet_name=SHEET_RECOV[:31], index=False)

    tmp.replace(out_path)
    print(f"\nwrote final roles:::::: -> {out_path}")


if __name__ == "__main__":
    main()

