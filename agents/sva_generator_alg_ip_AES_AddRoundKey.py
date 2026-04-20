import os, re, pathlib, logging
from typing import Dict, Tuple, Optional, List

logger = logging.getLogger(__name__)
GENERATED_DIR = os.path.join(".", "generated")
os.makedirs(GENERATED_DIR, exist_ok=True)

def _read(p: str) -> str:
    return pathlib.Path(p).read_text(encoding="utf-8", errors="ignore")

def _write(p: str, s: str) -> None:
    pathlib.Path(os.path.dirname(p)).mkdir(parents=True, exist_ok=True)
    pathlib.Path(p).write_text(s, encoding="utf-8")

def _drive_agnostic(path: str) -> str:
    return path.split(":", 1)[-1]

_ID = r"[A-Za-z_]\w*"

def _strip_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"//.*?$", "", s, flags=re.M)
    return s

def _norm(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip().lower())

def _dut_from_design(design_text: str, design_path: str) -> Optional[str]:
    design_stub = os.path.splitext(os.path.basename(_drive_agnostic(design_path)))[0]
    mods = re.findall(rf"\bmodule\s+({_ID})\b", design_text)
    if not mods: return None
    for m in mods:
        if m == design_stub: return m
    return mods[0]

def _dut_from_analysis(analysis_text: str) -> Optional[str]:
    m = re.search(r"best module.*?`([A-Za-z_]\w*)`", analysis_text or "", flags=re.I | re.S)
    return m.group(1) if m else None

def _parse_markdown_table(analysis_text: str) -> List[Dict[str, str]]:
    lines = [ln for ln in (analysis_text or "").splitlines() if "|" in ln]
    if not lines: return []
    header_idx, headers = None, None
    for i, ln in enumerate(lines):
        cells = [c.strip() for c in ln.split("|") if c.strip()!=""]
        if any("role" in c.lower() for c in cells):
            header_idx, headers = i, cells
            break
    if header_idx is None: return []
    hdrs = [_norm(h) for h in headers]
    data_lines = lines[header_idx+1:]
    if data_lines and re.match(r"^\s*\|?\s*[:-]{2,}", data_lines[0].replace("|","").strip()):
        data_lines = data_lines[1:]
    rows: List[Dict[str,str]] = []
    for ln in data_lines:
        cells = [c.strip() for c in ln.split("|") if c.strip()!=""]
        if len(cells) < 2: continue
        while len(cells) < len(hdrs): cells.append("")
        rows.append({ hdrs[i]: cells[i] for i in range(min(len(hdrs), len(cells))) })
    return rows

def _chosen_token(v: str) -> Optional[str]:
    s = (v or "").strip().strip("`")
    tok = re.split(r"[\s,]+", s)[0]
    return tok if tok and re.match(rf"^{_ID}$", tok) else None

def _width_is_128(v: str) -> bool:
    m = re.search(r"(\d+)", v or "")
    try:
        return int(m.group(1)) == 128 if m else False
    except Exception:
        return False

def _extract_ark_ios_from_analysis(analysis_text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    rows = _parse_markdown_table(analysis_text)
    sin: Optional[str] = None
    rk : Optional[str] = None
    sout: Optional[str] = None

    for r in rows:
        role = _norm(r.get("role",""))
        chosen = _chosen_token(r.get("chosen") or r.get("candidate(s)_in_design") or "")
        width  = r.get("width","")
        if not chosen: continue

        if re.search(r"(state_?in|in(?:put)?_?state)", role):
            if sin is None or _width_is_128(width): sin = chosen
        if re.search(r"(round_?key|rk|key_?in)", role):
            if rk is None or _width_is_128(width): rk = chosen
        if re.search(r"(state_?out|out(?:put)?_?state)", role):
            if sout is None or _width_is_128(width): sout = chosen

    if (sin is None or rk is None or sout is None) and rows:
        c128 = []
        for r in rows:
            chosen = _chosen_token(r.get("chosen") or r.get("candidate(s)_in_design") or "")
            if chosen and _width_is_128(r.get("width","")) and chosen not in c128:
                c128.append(chosen)
        if len(c128) >= 3:
            sin  = sin  or c128[0]
            rk   = rk   or c128[1]
            sout = sout or c128[2]

    return sin, rk, sout

_WRAPPER_TMPL = """`include "{design_base}"
module {wrapper_top};
  // no top-level drivers (hierarchical refs only)

  // DUT instance (explicitly named for hierarchical refs)
  {dut} u_dut(
    // no connections
  );

  // ---------- Assertions ----------
{sva}

endmodule
"""

def _build_sva(state_in: Optional[str], round_key: Optional[str], state_out: Optional[str]) -> str:
    si = f"u_dut.{state_in}"  if state_in  else "/*state_in*/'0"
    rk = f"u_dut.{round_key}" if round_key else "/*round_key*/'0"
    so = f"u_dut.{state_out}" if state_out else "/*state_out*/'0"
    return f"""// AddRoundKey: state_out == state_in ^ round_key (combinational check)
property p_addroundkey;
  ({so} == ({si} ^ {rk}));
endproperty
_assert_addroundkey: assert property (p_addroundkey);"""

def generate_sva_for_aes_addroundkey(
    *, category: str, property_name: str, design_path: str, analysis_path: str
) -> Tuple[str, str, str]:
    design_text   = _read(design_path)
    analysis_text = _read(analysis_path)

    dut = _dut_from_analysis(analysis_text) or _dut_from_design(design_text, design_path)
    if not dut:
        raise RuntimeError("Could not find a module header in the design.")

    state_in, round_key, state_out = _extract_ark_ios_from_analysis(analysis_text)
    sva_text = _build_sva(state_in, round_key, state_out)

    wrapper_top = f"{dut}__{property_name}_wrapper"
    design_base = os.path.basename(_drive_agnostic(design_path))
    wrapper_sv  = _WRAPPER_TMPL.format(
        design_base=design_base,
        wrapper_top=wrapper_top,
        dut=dut,
        sva=sva_text.strip(),
    )

    sv_out = os.path.join(GENERATED_DIR, f"{os.path.splitext(design_base)[0]}__{property_name}_wrapper.sv")
    _write(sv_out, wrapper_sv)

    tcl = "\n".join([
        "clear -all",
        f"analyze -sv12 {os.path.basename(sv_out)}",
        f"elaborate -top {wrapper_top} -create_related_covers witness",
        "clock -none",
        "reset -none",
        "prove -all",
        "",
    ])
    tcl_out = os.path.join(GENERATED_DIR, f"{property_name}_algo.tcl")
    _write(tcl_out, tcl)

    logger.info("Generated wrapper: %s", sv_out)
    logger.info("Generated tcl    : %s", tcl_out)
    return sv_out, tcl_out, wrapper_top
