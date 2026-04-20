import argparse
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd


# def _read_roles_xlsx(path: str, sheet: Optional[str] = None) -> Dict[str, str]:
#     xl = pd.ExcelFile(path)
#     use_sheet = sheet if sheet and sheet in xl.sheet_names else xl.sheet_names[0]
#     df = pd.read_excel(path, sheet_name=use_sheet)
#     df.columns = [str(c).strip() for c in df.columns]
#     if "Role" not in df.columns or "Chosen" not in df.columns:
#         raise ValueError(f"{path}: expected columns Role, Chosen (sheet {use_sheet})")
#     out: Dict[str, str] = {}
#     for _, r in df.iterrows():
#         role = str(r.get("Role", "")).strip()
#         chosen = str(r.get("Chosen", "")).strip()
#         if role and chosen and chosen.lower() != "nan":
#             out[role] = chosen
#     return out

def _read_roles_xlsx(path: str, sheet: Optional[str] = None) -> Dict[str, str]:
    xl = pd.ExcelFile(path)
    use_sheet = sheet if sheet and sheet in xl.sheet_names else xl.sheet_names[0]
    df = pd.read_excel(path, sheet_name=use_sheet)
    # normalize to lowercase so "Role", "role", "ROLE" all work
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "role" not in df.columns or "chosen" not in df.columns:
        raise ValueError(
            f"{path} (sheet={use_sheet}): could not find role/chosen columns. "
            f"Found: {list(df.columns)}"
        )
    out: Dict[str, str] = {}
    for _, r in df.iterrows():
        role   = str(r.get("role",   "")).strip()
        chosen = str(r.get("chosen", "")).strip().strip("`")  # strip backticks
        if role and chosen and chosen.lower() != "nan":
            out[role] = chosen
    return out
def _sanitize_ident(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", s or "")
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "wrapper"

def _extract_enum_blocks(sv_text: str) -> List[Tuple[Optional[int], str, Dict[str, str]]]:
    blocks: List[Tuple[Optional[int], str, Dict[str, str]]] = []
    rx = re.compile(r"typedef\s+enum\b(?P<head>[^{};]*)\{(?P<body>.*?)\}\s*(?P<tname>\w+)\s*;",
                    re.S | re.I)
    for m in rx.finditer(sv_text):
        head = m.group("head") or ""
        body = m.group("body") or ""
        w = None
        mw = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", head)
        if mw:
            msb = int(mw.group(1))
            lsb = int(mw.group(2))
            w = abs(msb - lsb) + 1

        mapping: Dict[str, str] = {}
        parts = [p.strip() for p in re.split(r",(?![^{}]*\})", body) if p.strip()]
        for p in parts:
            p2 = re.sub(r"//.*", "", p).strip()
            p2 = re.sub(r"/\*.*?\*/", "", p2, flags=re.S).strip()
            if not p2:
                continue
            mm = re.match(r"(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<val>[^,]+)$", p2)
            if mm:
                mapping[mm.group("name")] = mm.group("val").strip()
        blocks.append((w, body, mapping))
    return blocks


def _pick_enum_block_for_literals(blocks: List[Tuple[Optional[int], str, Dict[str, str]]],
                                 needed: List[str]) -> Tuple[Optional[int], Dict[str, str]]:
    needed_set = set(needed)
    if not blocks:
        return None, {}
    best = None
    best_cov = -1
    for w, _, mp in blocks:
        cov = len(needed_set & set(mp.keys()))
        if cov > best_cov:
            best_cov = cov
            best = (w, mp)
        if cov == len(needed_set):
            return w, mp
    return best if best else (None, {})


def _format_localparams(enum_width: Optional[int],
                        enum_map: Dict[str, str],
                        literals: List[str]) -> str:
    if not literals or not enum_map:
        return ""
    lines = []
    for lit in literals:
        if lit not in enum_map:
            continue
        val = enum_map[lit]
        if enum_width is not None:
            lines.append(f"  localparam logic [{enum_width-1}:0] {lit} = {val};")
        else:
            lines.append(f"  localparam {lit} = {val};")
    return "\n".join(lines) + ("\n" if lines else "")



def _property_body(prop: str,
                   clk: str,
                   rst: str,
                   state: str,
                   next_state: str,
                   safe_state: str,
                   legal_list: List[str]) -> str:
    legal = ", ".join(legal_list)
    if prop == "always_legal_state":
        return f"""property always_legal_state;
  @(posedge u_dut.{clk}) disable iff (u_dut.{rst})
    (u_dut.{state} inside {{{legal}}}) && !$isunknown(u_dut.{state});
endproperty
_assert_1: assert property (always_legal_state);"""
    if prop == "recovery_from_illegal_state":
        return f"""property recovery_from_illegal_state;
  @(posedge u_dut.{clk}) disable iff (u_dut.{rst})
    ( !(u_dut.{state} inside {{{legal}}}) || $isunknown(u_dut.{state}) )
    |-> (u_dut.{next_state} == {safe_state}) ##1 (u_dut.{state} == {safe_state});
endproperty
_assert_1: assert property (recovery_from_illegal_state);"""
    raise ValueError(f"Unknown FSM property: {prop}")


def generate(*,
             design_sv: str,
             roles_xlsx: str,
             prop_name: str,
             outdir: str,
             roles_sheet: Optional[str] = None) -> Dict[str, str]:
    roles = _read_roles_xlsx(roles_xlsx, sheet=roles_sheet)

    dut = roles.get("dut", "").strip() or Path(design_sv).stem
    clk = roles.get("clk", "clk").strip()
    #rst = roles.get("rst", "rst").strip()
    rst = (roles.get("rst") or roles.get("reset") or roles.get("reset_disable") or "rst").strip()
    state = roles.get("state", "state").strip()
    next_state = roles.get("next_state", "next_state").strip()
    safe_state = roles.get("safe_state", "").strip()
    legal_set = roles.get("legal_set", "").strip()

    legal_list = [x.strip() for x in legal_set.split(",") if x.strip()] if legal_set else []
    if not legal_list:
        if safe_state:
            legal_list = [safe_state]

    sv_text = Path(design_sv).read_text(encoding="utf-8", errors="ignore")
    blocks = _extract_enum_blocks(sv_text)
    enum_width, enum_map = _pick_enum_block_for_literals(blocks, legal_list)

    if not safe_state and legal_list:
        safe_state = legal_list[0]

    localparam_text = _format_localparams(enum_width, enum_map, legal_list)

    wrapper_mod = _sanitize_ident(f"{dut}__{prop_name}_wrapper")
    wrapper_file = Path(outdir) / f"{wrapper_mod}.sv"
    tcl_file = Path(outdir) / f"{wrapper_mod}_run.tcl"

    Path(outdir).mkdir(parents=True, exist_ok=True)

    wrapper = f"""`include \"{Path(design_sv).name}\"
module {wrapper_mod};
  logic {clk};
  logic {rst};

  {dut} u_dut(
    .{clk}({clk}),
    .{rst}({rst})
  );

{localparam_text}  // ---------- Assertions ----------
{_property_body(prop_name, clk, rst, state, next_state, safe_state, legal_list)}

endmodule
"""
    wrapper_file.write_text(wrapper, encoding="utf-8")

    tcl = "\n".join([
        "clear -all",
        "",
        f"analyze -sv12 {wrapper_file.name}",
        f"elaborate -top {wrapper_mod} -create_related_covers witness",
        "",
        f"clock {clk}",
        f"reset {rst}",
        "",
        "prove -all",
        "",
    ])
    tcl_file.write_text(tcl, encoding="utf-8")

    return {"wrapper_sv": str(wrapper_file), "run_tcl": str(tcl_file)}


def main():
    ap = argparse.ArgumentParser(description="FSM SVA generator (algo) from final_roles_*.xlsx")
    ap.add_argument("--design_sv", required=True, help="Path to DUT SystemVerilog file (e.g., fsm_01.sv)")
    ap.add_argument("--roles_xlsx", required=True, help="Path to final_roles_<label>.xlsx (p1 or p2 folder)")
    ap.add_argument("--property", required=True, choices=["always_legal_state", "recovery_from_illegal_state"],
                    help="FSM property to generate")
    ap.add_argument("--outdir", required=True, help="Output folder for wrapper + tcl")
    ap.add_argument("--roles_sheet", default=None, help="Optional sheet name in roles xlsx (default: first sheet)")
    args = ap.parse_args()

    paths = generate(
        design_sv=args.design_sv,
        roles_xlsx=args.roles_xlsx,
        prop_name=args.property,
        outdir=args.outdir,
        roles_sheet=args.roles_sheet,
    )
    print(f"[OK] wrapper: {paths['wrapper_sv']}")
    print(f"[OK] tcl    : {paths['run_tcl']}")

def generate_sva_for_fsm_property(
    *, category: str, property_name: str, design_path: str, analysis_path: str
) -> Tuple[str, str, str]:
    label = Path(design_path).stem          
    roles_xlsx = f"static_mod_new/FSM/final_roles_{label}.xlsx"
    outdir     = f"generated/FSM/{label}"

    if not Path(roles_xlsx).exists():
        raise FileNotFoundError(
            f"final_roles not found: {roles_xlsx}\n"
            f"Run fsm_pairs_prune.py first for label '{label}'."
        )

    paths = generate(
        design_sv=design_path,
        roles_xlsx=roles_xlsx,
        prop_name=property_name,
        outdir=outdir,
        roles_sheet=property_name,
    )
    return paths["wrapper_sv"], paths["run_tcl"], property_name

if __name__ == "__main__":

    main()


