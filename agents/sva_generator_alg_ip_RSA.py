from __future__ import annotations

import os
import re
import pathlib
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

GENERATED_DIR = os.path.join(".", "generated")
os.makedirs(GENERATED_DIR, exist_ok=True)

_ID = r"[A-Za-z_]\w*"


def _read(p: str) -> str:
    return pathlib.Path(p).read_text(encoding="utf-8", errors="ignore")

def _write(p: str, s: str) -> None:
    pathlib.Path(os.path.dirname(p)).mkdir(parents=True, exist_ok=True)
    pathlib.Path(p).write_text(s, encoding="utf-8")

def _abs_fwd(p: str) -> str:
    return os.path.abspath(p).replace("\\", "/")

def _strip_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"//.*?$", "", s, flags=re.M)
    return s

def _norm(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip().lower())

def _chosen_token(v: str) -> Optional[str]:
    s = (v or "").strip().strip("`").strip()
    tok = re.split(r"[\s,]+", s)[0] if s else ""
    return tok if tok and re.match(rf"^{_ID}$", tok) else None


def _sdi_bigram(a: str, b: str) -> float:
    a = (a or "").lower()
    b = (b or "").lower()
    if not a or not b:
        return 0.0

    def bigr(s: str) -> set:
        return {s[i:i+2] for i in range(len(s)-1)} if len(s) > 1 else {s}

    A = bigr(a)
    B = bigr(b)
    return 0.0 if (not A or not B) else (2.0 * len(A & B) / (len(A) + len(B)))

def _roles_from_final_roles_xlsx(xlsx_path: str, func_hint: str = "top_module") -> Dict[str, str]:

    try:
        import pandas as pd
    except Exception as e:
        raise RuntimeError(f"pandas is required to read Excel roles file: {e}")

    xl = pd.ExcelFile(xlsx_path)
    sheet = func_hint if func_hint in xl.sheet_names else xl.sheet_names[0]
    df = pd.read_excel(xlsx_path, sheet_name=sheet)

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    colmap = {str(c).strip().lower(): str(c).strip() for c in df.columns}

    role_col = colmap.get("role")
    chosen_col = colmap.get("chosen")
    func_col = colmap.get("function")  

    if not role_col or not chosen_col:
        raise RuntimeError(
            f"Excel roles file {xlsx_path} must contain columns Role and Chosen. "
            f"Found columns: {list(df.columns)}"
        )

    if func_col:
        df2 = df[df[func_col].astype(str).str.strip().str.lower() == func_hint.lower()].copy()
        if not df2.empty:
            df = df2

    roles: Dict[str, str] = {}
    for _, r in df.iterrows():
        role = _norm(str(r.get(role_col, "")).strip())
        chosen = _chosen_token(str(r.get(chosen_col, "")).strip())
        if role and chosen:
            roles[role] = chosen

    return roles

def _split_pipe_row(line: str) -> List[str]:
    core = (line or "").strip()
    if not core:
        return []
    if core.startswith("|"):
        core = core[1:]
    if core.endswith("|"):
        core = core[:-1]
    return [c.strip() for c in core.split("|")]

def _is_separator_row(line: str) -> bool:
    core = (line or "").strip()
    if not core or "|" not in core:
        return False
    core2 = core.replace("|", "").replace(" ", "")
    return bool(core2) and re.fullmatch(r"[:\-]+", core2) is not None

def _parse_markdown_table(analysis_text: str) -> List[Dict[str, str]]:
    lines = (analysis_text or "").splitlines()
    header_i = None
    header_cells: List[str] = []

    for i, ln in enumerate(lines):
        if "|" not in ln:
            continue
        cells = _split_pipe_row(ln)
        if any(c.lower() == "role" or "role" in c.lower() for c in cells):
            header_i = i
            header_cells = cells
            break
    if header_i is None:
        return []

    headers = [_norm(h) for h in header_cells]
    rows: List[Dict[str, str]] = []

    j = header_i + 1
    if j < len(lines) and _is_separator_row(lines[j]):
        j += 1

    while j < len(lines):
        ln = lines[j]
        if not ln.strip() or "|" not in ln:
            break
        if _is_separator_row(ln):
            j += 1
            continue

        cells = _split_pipe_row(ln)
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        elif len(cells) > len(headers):
            cells = cells[:len(headers)]

        row = {headers[k]: cells[k] for k in range(len(headers))}
        if any(v.strip() for v in row.values()):
            rows.append(row)
        j += 1

    return rows

def _roles_from_analysis_txt(analysis_text: str) -> Dict[str, str]:
    roles: Dict[str, str] = {}
    for r in _parse_markdown_table(analysis_text):
        role = _norm(r.get("role", ""))
        chosen = _chosen_token(r.get("chosen", ""))
        if role and chosen:
            roles[role] = chosen
    return roles

def _dut_from_analysis_txt(analysis_text: str) -> Optional[str]:
    m = re.search(r"best\s+module.*?`([A-Za-z_]\w*)`", analysis_text or "", flags=re.I | re.S)
    if m:
        return m.group(1)
    roles = _roles_from_analysis_txt(analysis_text)
    return roles.get("dut")

@dataclass
class PortInfo:
    name: str
    direction: str = ""
    width_bits: Optional[int] = None
    raw_decl: str = ""

def _find_module_block(text: str, dut: str) -> Optional[Tuple[int, int]]:
    rx = re.compile(rf"\bmodule\s+{re.escape(dut)}\b", re.I)
    m = rx.search(text)
    if not m:
        return None
    start = m.start()
    mend = re.search(r"\bendmodule\b", text[m.end():], flags=re.I | re.S)
    end = m.end() + (mend.end() if mend else len(text))
    return start, end

def _compute_width_bits(txt: str) -> Optional[int]:
    m = re.search(r"\[\s*([0-9]+)\s*:\s*([0-9]+)\s*\]", txt or "")
    if not m:
        return None
    msb = int(m.group(1))
    lsb = int(m.group(2))
    return abs(msb - lsb) + 1

def _extract_header_portlist(module_text: str, dut: str) -> Optional[str]:
    m = re.search(rf"\bmodule\s+{re.escape(dut)}\b", module_text, flags=re.I)
    if not m:
        return None
    i = m.end()
    while i < len(module_text) and module_text[i].isspace():
        i += 1

    if i < len(module_text) and module_text[i] == "#":
        j = module_text.find("(", i)
        if j == -1:
            return None
        depth = 0
        k = j
        while k < len(module_text):
            if module_text[k] == "(":
                depth += 1
            elif module_text[k] == ")":
                depth -= 1
                if depth == 0:
                    i = k + 1
                    break
            k += 1

    j = module_text.find("(", i)
    if j == -1:
        return None
    depth = 0
    k = j
    while k < len(module_text):
        ch = module_text[k]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return module_text[j+1:k]
        k += 1
    return None

def _split_top_level_commas(s: str) -> List[str]:
    parts = []
    cur = []
    dp = 0
    db = 0
    for ch in s:
        if ch == "(":
            dp += 1
        elif ch == ")":
            dp = max(0, dp - 1)
        elif ch == "[":
            db += 1
        elif ch == "]":
            db = max(0, db - 1)

        if ch == "," and dp == 0 and db == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)

    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]

def _parse_ansi_port_decl(item: str) -> Optional[PortInfo]:
    raw = (item or "").strip()
    if not raw:
        return None
    raw2 = raw.split("=", 1)[0].strip()
    md = re.search(r"\b(input|output|inout)\b", raw2, flags=re.I)
    direction = md.group(1).lower() if md else ""
    mn = re.search(rf"({_ID})\s*$", raw2)
    if not mn:
        return None
    name = mn.group(1)
    width_bits = _compute_width_bits(raw2)
    return PortInfo(name=name, direction=direction, width_bits=width_bits, raw_decl=raw)

def _parse_nonansi_port_names(portlist: str) -> List[str]:
    names = []
    for item in _split_top_level_commas(portlist or ""):
        item = item.strip()
        if not item:
            continue
        m = re.match(rf"^({_ID})$", item)
        if m:
            names.append(m.group(1))
    return names

def _infer_decls_in_body(module_text: str, port_names: List[str]) -> Dict[str, PortInfo]:
    out: Dict[str, PortInfo] = {}
    for pn in port_names:
        rx = re.compile(rf"\b(input|output|inout)\b[^;]*\b{re.escape(pn)}\b", re.I)
        m = rx.search(module_text)
        if not m:
            out[pn] = PortInfo(name=pn, direction="", width_bits=None, raw_decl="")
            continue
        decl = m.group(0)
        direction = m.group(1).lower()
        width_bits = _compute_width_bits(decl)
        out[pn] = PortInfo(name=pn, direction=direction, width_bits=width_bits, raw_decl=decl)
    return out

def _extract_ports(design_text: str, dut: str) -> Dict[str, PortInfo]:
    txt = _strip_comments(design_text)
    span = _find_module_block(txt, dut)
    if not span:
        return {}
    mod_text = txt[span[0]:span[1]]
    portlist = _extract_header_portlist(mod_text, dut) or ""
    items = _split_top_level_commas(portlist)

    if any(re.search(r"\b(input|output|inout)\b", it, re.I) for it in items):
        ports: Dict[str, PortInfo] = {}
        for it in items:
            pi = _parse_ansi_port_decl(it)
            if pi:
                ports[pi.name] = pi
        return ports

    names = _parse_nonansi_port_names(portlist)
    return _infer_decls_in_body(mod_text, names)

def _dut_from_design(design_text: str, design_path: str) -> Optional[str]:
    design_stub = os.path.splitext(os.path.basename(design_path))[0]
    mods = re.findall(rf"\bmodule\s+({_ID})\b", design_text)
    if not mods:
        return None
    for m in mods:
        if m.lower() == design_stub.lower():
            return m
    return mods[0]


_CLK_ALIASES = ["clk", "clock", "clk_i", "clk_in", "i_clk", "aclk", "core_clk", "ap_clk"]
_RST_ALIASES = ["rst", "reset", "rst_i", "reset_i", "rst_n", "reset_n", "aresetn", "rstni", "resetb", "rstb", "nreset"]

def _pick_best_port(ports: Dict[str, PortInfo], aliases: List[str]) -> Optional[str]:
    if not ports:
        return None
    best = None
    best_sc = -1.0
    alias_set = {a.lower() for a in aliases}
    for name, pi in ports.items():
        if pi.direction and pi.direction != "input":
            continue
        sc = 10.0 if name.lower() in alias_set else max(_sdi_bigram(name, a) for a in aliases)
        if sc > best_sc:
            best_sc = sc
            best = name
    return best

def _is_active_low_reset(rst_name: str) -> bool:
    n = (rst_name or "").lower()
    if n in {"rst_n", "reset_n", "aresetn", "rstni", "resetb", "rstb"}:
        return True
    if re.search(r"(rst|reset).*_n\b", n):
        return True
    return False

_WRAPPER_TMPL = """`include "{design_include}"
module {wrapper_top};
{decls}

  // DUT instance (explicitly named for hierarchical refs)
  {dut} u_dut(
{port_conns}
  );

  // ---------- Assertions ----------
{svacode}

endmodule
"""

def _build_tcl(wrapper_path: str, wrapper_top: str, clk_sig: str, rst_sig: Optional[str]) -> str:
    wp = "{" + _abs_fwd(wrapper_path) + "}"
    lines = [
        "clear -all",
        "",
        f"analyze -sv12 {wp}",
        f"elaborate -top {wrapper_top} -create_related_covers witness",
        "",
        f"clock {clk_sig}",
    ]
    if rst_sig:
        lines.append(f"reset {rst_sig}")
    else:
        lines.append("reset -none")
    lines += ["", "prove -all", ""]
    return "\n".join(lines)


def generate_sva_for_rsa_property(
    *, category: str, property_name: str, design_path: str, analysis_path: str
) -> Tuple[str, str, str]:
    design_text = _read(design_path)

    roles: Dict[str, str] = {}
    dut: Optional[str] = None

    ap = (analysis_path or "").strip()
    if ap and os.path.exists(ap) and os.path.splitext(ap)[1].lower() in {".xlsx", ".xls", ".xlsm"}:
        roles = _roles_from_final_roles_xlsx(ap, func_hint="top_module")
        dut = roles.get("dut")
    else:
        analysis_text = _read(ap) if ap and os.path.exists(ap) else ""
        roles = _roles_from_analysis_txt(analysis_text)
        dut = _dut_from_analysis_txt(analysis_text) or roles.get("dut")

    if not dut:
        dut = _dut_from_design(design_text, design_path)
    if not dut:
        raise RuntimeError("Could not determine DUT module name from final_roles Excel/TXT or design.")

    msg = roles.get("message")
    ciph = roles.get("cipher")
    if not msg or not ciph:
        raise RuntimeError(
            "Roles file must contain roles 'message' and 'cipher' with chosen signals.\n"
            f"Found roles: {sorted(list(roles.keys()))}\n"
            f"analysis_path={analysis_path}"
        )

    clk_hint = roles.get("clk")
    rst_hint = roles.get("rst")

    ports = _extract_ports(design_text, dut)

    clk_port = clk_hint if (clk_hint and clk_hint in ports) else _pick_best_port(ports, _CLK_ALIASES)
    rst_port = rst_hint if (rst_hint and rst_hint in ports) else _pick_best_port(ports, _RST_ALIASES)

    if not clk_port:
        clk_port = clk_hint or "clk"

    active_low = _is_active_low_reset(rst_port) if rst_port else False

    u = "u_dut"
    if rst_port:
        rst_expr = f"!{u}.{rst_port}" if active_low else f"{u}.{rst_port}"
    else:
        rst_expr = "1'b0"

    sva = f"""property {property_name};
  @(posedge {u}.{clk_port}) disable iff ({rst_expr})
    ##[1:$] ({u}.{ciph} != {u}.{msg});
endproperty
_assert_1: assert property ({property_name});"""

    decl_lines = [f"  logic {clk_port};"]
    if rst_port:
        decl_lines.append(f"  logic {rst_port};")
    decls = "\n".join(decl_lines)

    conns: List[str] = []
    if clk_port and (clk_port in ports or re.match(rf"^{_ID}$", clk_port)):
        conns.append(f"    .{clk_port}({clk_port})")

    if rst_port and (rst_port in ports or re.match(rf"^{_ID}$", rst_port)):
        conns.append(f"    .{rst_port}({rst_port})")

    port_conns = ",\n".join(conns) if conns else "    /* no port connections */"

    wrapper_top = f"{dut}__{property_name}_wrapper"
    wrapper_sv = _WRAPPER_TMPL.format(
        design_include=_abs_fwd(design_path),
        wrapper_top=wrapper_top,
        dut=dut,
        decls=decls,
        port_conns=port_conns,
        svacode=sva.strip(),
    )

    design_stem = os.path.splitext(os.path.basename(design_path))[0]
    sv_out = os.path.join(GENERATED_DIR, f"{design_stem}__{property_name}_wrapper.sv")
    _write(sv_out, wrapper_sv)

    tcl = _build_tcl(wrapper_path=sv_out, wrapper_top=wrapper_top, clk_sig=clk_port, rst_sig=rst_port)
    tcl_out = os.path.join(GENERATED_DIR, f"{property_name}_algo.tcl")
    _write(tcl_out, tcl)

    logger.info("RSA algo wrapper: %s", os.path.abspath(sv_out))
    logger.info("RSA algo tcl    : %s", os.path.abspath(tcl_out))
    logger.info(
        "DUT=%s | message=%s | cipher=%s | clk_port=%s | rst_port=%s (active_low=%s) | roles_file=%s",
        dut, msg, ciph, clk_port, (rst_port or "<none>"), active_low, analysis_path
    )


    if rst_port and active_low:
        logger.warning(
            "Reset port '%s' looks active-low. Property uses disable iff(!u_dut.%s). "
            "If your Jasper flow needs explicit reset polarity, adjust the TCL reset command.",
            rst_port, rst_port
        )

    return sv_out, tcl_out, wrapper_top

