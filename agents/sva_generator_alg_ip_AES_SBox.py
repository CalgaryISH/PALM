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

def _strip_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)  
    s = re.sub(r"//.*?$", "", s, flags=re.M)     
    return s

def _norm(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip().lower())

def _dut_from_design(design_text: str, design_path: str) -> Optional[str]:
    design_stub = os.path.splitext(os.path.basename(_drive_agnostic(design_path)))[0]
    mods = re.findall(rf"\bmodule\s+([A-Za-z_]\w*)\b", design_text)
    if not mods:
        return None
    for m in mods:
        if m == design_stub:
            return m
    return mods[0]

def _dut_from_analysis(analysis_text: str) -> Optional[str]:
    m = re.search(r"best module.*?`([A-Za-z_]\w*)`", analysis_text or "", flags=re.I | re.S)
    return m.group(1) if m else None

def _parse_markdown_table(analysis_text: str) -> List[Dict[str, str]]:
    lines = [ln for ln in (analysis_text or "").splitlines() if "|" in ln]
    if not lines:
        return []
    header_idx, headers = None, None
    for i, ln in enumerate(lines):
        cells = [c.strip() for c in ln.split("|") if c.strip()!=""]
        if any("role" in c.lower() for c in cells):
            header_idx, headers = i, cells
            break
    if header_idx is None:
        return []
    hdrs = [_norm(h) for h in headers]
    data_lines = lines[header_idx+1:]
    if data_lines and re.match(r"^\s*\|?\s*[:-]{2,}", data_lines[0].replace("|","").strip()):
        data_lines = data_lines[1:]
    rows: List[Dict[str,str]] = []
    for ln in data_lines:
        cells = [c.strip() for c in ln.split("|") if c.strip()!=""]
        if len(cells) < 2:
            continue
        while len(cells) < len(hdrs):
            cells.append("")
        row = { hdrs[i]: cells[i] for i in range(min(len(hdrs), len(cells))) }
        rows.append(row)
    return rows

def _extract_sbox_io_from_analysis(analysis_text: str) -> Tuple[Optional[str], Optional[str]]:

    rows = _parse_markdown_table(analysis_text)
    in_sig: Optional[str] = None
    out_sig: Optional[str] = None

    def _chosen_token(v: str) -> Optional[str]:
        s = (v or "").strip().strip("`")
        tok = re.split(r"[\s,]+", s)[0]
        return tok if tok and re.match(rf"^{_ID}$", tok) else None

    for r in rows:
        role = _norm(r.get("role",""))
        chosen = _chosen_token(r.get("chosen") or r.get("candidate(s)_in_design") or "")
        if not chosen:
            continue
        if re.search(r"(?:^|_)in(?:put)?(?:_|$)", role):
            in_sig = in_sig or chosen
        if re.search(r"(?:^|_)out(?:put)?(?:_|$)", role):
            out_sig = out_sig or chosen

    if (in_sig is None or out_sig is None) and rows:
        toks = []
        for r in rows:
            c = _chosen_token(r.get("chosen") or r.get("candidate(s)_in_design") or "")
            if c and c not in toks:
                toks.append(c)
        if len(toks) >= 2:
            in_sig = in_sig or toks[0]
            out_sig = out_sig or toks[1]

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

def generate_sva_for_aes_property(
    *, category: str, property_name: str,
    design_path: str, analysis_path: str
):
    design_text   = _read(design_path)
    analysis_text = _read(analysis_path)

    dut = _dut_from_analysis(analysis_text) or _dut_from_design(design_text, design_path)
    if not dut:
        raise RuntimeError("Could not find a module header in the design.")
    in_sig, out_sig = _extract_sbox_io_from_analysis(analysis_text)

    tmpl = _pick_template(_load_templates(category), property_name)

    subs = {
        "hdr": "",  
        "in":  (f"u_dut.{in_sig}"  if in_sig  else "/*data_i*/'0"),
        "out": (f"u_dut.{out_sig}" if out_sig else "/*data_o*/'0"),
        "data_in":  (f"u_dut.{in_sig}"  if in_sig  else "/*data_i*/'0"),
        "data_out": (f"u_dut.{out_sig}" if out_sig else "/*data_o*/'0"),
        "clk": "",
        "reset": "",
        "reset_disable": "",
    }

    if tmpl and "sva_template" in tmpl:
        try:
            sva_text = tmpl["sva_template"].format(**subs)
        except Exception as e:
            logger.warning("AES SBox template formatting issue: %s; falling back to default.", e)
            tmpl = None

    if not tmpl:
        sva_text = f"""// AES SBox sanity: output must not be equal to input or its bitwise inverse
property s_box;
    ({subs['out']} != {subs['in']}) && ({subs['out']} != ~{subs['in']});
endproperty
assert_sbox: assert property (s_box);"""

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
