import os, re, yaml, pathlib, logging
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

def _load_templates(category: str) -> List[dict]:
    for ext in (".yaml", ".yml"):
        p = pathlib.Path(f"data/templates/{category}{ext}")
        if p.exists():
            data = yaml.safe_load(_read(p)) or []
            if isinstance(data, list):
                return data
            raise ValueError(f"Template file must contain a YAML list, got {type(data).__name__}")
    return []

def _pick_template(templates: List[dict], property_name: str) -> Optional[dict]:
    for t in templates:
        if str(t.get("name","")).lower() == str(property_name).lower():
            return t
    return None

_ID = r"[A-Za-z_]\w*"

def _norm(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip().lower())

def _dut_from_design(design_text: str, design_path: str) -> Optional[str]:
    design_stub = os.path.splitext(os.path.basename(_drive_agnostic(design_path)))[0]
    mods = re.findall(rf"\bmodule\s+([A-Za-z_]\w*)\b", design_text)
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

def _extract_sr_io_from_analysis(analysis_text: str) -> Tuple[Optional[str], Optional[str]]:

    rows = _parse_markdown_table(analysis_text)
    in_sig: Optional[str] = None
    out_sig: Optional[str] = None

    for r in rows:
        role = _norm(r.get("role",""))
        chosen = _chosen_token(r.get("chosen") or r.get("candidate(s)_in_design") or "")
        width  = r.get("width","")
        if not chosen: continue
        if re.search(r"(?:^|_)in(?:put)?(?:_|$)", role):
            if in_sig is None or _width_is_128(width):
                in_sig = chosen
        if re.search(r"(?:^|_)out(?:put)?(?:_|$)", role):
            if out_sig is None or _width_is_128(width):
                out_sig = chosen

    if (in_sig is None or out_sig is None) and rows:
        cands = []
        for r in rows:
            chosen = _chosen_token(r.get("chosen") or r.get("candidate(s)_in_design") or "")
            width  = r.get("width","")
            if chosen and _width_is_128(width) and chosen not in cands:
                cands.append(chosen)
        if len(cands) >= 2:
            in_sig  = in_sig  or cands[0]
            out_sig = out_sig or cands[1]

    return in_sig, out_sig

_WRAPPER_TMPL = """{include_line}module {wrapper_top};
  // no top-level drivers (hierarchical refs only)

  // DUT instance (explicitly named for hierarchical refs)
  {dut} u_dut(
    // no connections
  );

  // ---------- Assertions ----------
{sva}

endmodule
"""

def generate_sva_for_aes_shiftrows(
    *, category: str, property_name: str,
    design_path: str, analysis_path: str
):
    design_text   = _read(design_path)
    analysis_text = _read(analysis_path)

    dut = _dut_from_analysis(analysis_text) or _dut_from_design(design_text, design_path)
    if not dut:
        raise RuntimeError("Could not find a module header in the design.")

    in_sig, out_sig = _extract_sr_io_from_analysis(analysis_text)
    tmpl = _pick_template(_load_templates(category), property_name)

    subs = {
        "hdr": "",  
        "input_data":  (f"u_dut.{in_sig}"  if in_sig  else "/*input_data*/'0"),
        "output_data": (f"u_dut.{out_sig}" if out_sig else "/*output_data*/'0"),
        "in":  (f"u_dut.{in_sig}"  if in_sig  else "/*input_data*/'0"),
        "out": (f"u_dut.{out_sig}" if out_sig else "/*output_data*/'0"),
        "data_in":  (f"u_dut.{in_sig}"  if in_sig  else "/*input_data*/'0"),
        "data_out": (f"u_dut.{out_sig}" if out_sig else "/*output_data*/'0"),
    }

    if tmpl and "sva_template" in tmpl:
        try:
            sva_text = tmpl["sva_template"].format(**subs)
        except Exception as e:
            logger.warning("AES ShiftRows template formatting issue: %s; falling back to built-in.", e)
            tmpl = None

    if not tmpl:
        idn = subs["input_data"]; odn = subs["output_data"]
        sva_text = f"""property p_shiftrows;
    (
      {odn}[127:120] == {idn}[127:120] &&
      {odn}[ 95: 88] == {idn}[ 95: 88] &&
      {odn}[ 63: 56] == {idn}[ 63: 56] &&
      {odn}[ 31: 24] == {idn}[ 31: 24]  &&

      {odn}[119:112] == {idn}[ 87: 80] &&
      {odn}[ 87: 80] == {idn}[ 55: 48] &&
      {odn}[ 55: 48] == {idn}[ 23: 16] &&
      {odn}[ 23: 16] == {idn}[119:112] &&

      {odn}[111:104] == {idn}[ 47: 40] &&
      {odn}[ 79: 72] == {idn}[ 15:  8] &&
      {odn}[ 47: 40] == {idn}[111:104] &&
      {odn}[ 15:  8] == {idn}[ 79: 72] &&

      {odn}[103: 96] == {idn}[  7:  0] &&
      {odn}[ 71: 64] == {idn}[103: 96] &&
      {odn}[ 39: 32] == {idn}[ 71: 64] &&
      {odn}[  7:  0] == {idn}[ 39: 32]
    );
endproperty
_assert_shiftrows: assert property (p_shiftrows);"""

    wrapper_top = f"{dut}__{property_name}_wrapper"
    design_base = os.path.basename(_drive_agnostic(design_path))
    include_line = f'`include "{design_base}"\n'
    wrapper_sv  = _WRAPPER_TMPL.format(
        include_line=include_line,
        wrapper_top=wrapper_top,
        dut=dut,
        sva=sva_text.strip(),
    )

    sv_out  = os.path.join(GENERATED_DIR, f"{os.path.splitext(design_base)[0]}__{property_name}_wrapper.sv")
    _write(sv_out, wrapper_sv)

    tcl_lines = [
        "clear -all",
        "",
        f"analyze -sv12 {os.path.basename(sv_out)}",
        f"elaborate -top {wrapper_top} -create_related_covers witness",
        "",
        "clock -none",
        "reset -none",
        "",
        "prove -all",
        "",
    ]
    tcl_out = os.path.join(GENERATED_DIR, f"{property_name}_algo.tcl")
    _write(tcl_out, "\n".join(tcl_lines))

    logger.info("Generated wrapper: %s", sv_out)
    logger.info("Generated tcl    : %s", tcl_out)
    return sv_out, tcl_out, wrapper_top
