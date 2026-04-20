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
def _drive_agnostic(p: str) -> str:
    return p.split(":")[-1]

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
    return re.sub(r"\s+", "_", (s or "").strip().lower())
def _token_present(s: str, name: Optional[str]) -> bool:
    if not name: return False
    return bool(re.search(rf"\b{name}\b", s))

def _roles_from_table(analysis_text: str) -> Dict[str, Dict[str, str]]:
    lines = [ln for ln in (analysis_text or "").splitlines() if "|" in ln]
    if not lines: return {}
    header_idx, headers = None, None
    for i, ln in enumerate(lines):
        cells = [c.strip() for c in ln.split("|") if c.strip()!=""]
        if any("role" in c.lower() for c in cells):
            header_idx, headers = i, cells
            break
    if header_idx is None: return {}
    hdrs = [_norm(h) for h in headers]
    data_lines = lines[header_idx+1:]
    if data_lines and re.match(r"^\s*\|?\s*[:-]{2,}", data_lines[0].replace("|","").strip()):
        data_lines = data_lines[1:]
    out: Dict[str, Dict[str, str]] = {}
    for ln in data_lines:
        cells = [c.strip() for c in ln.split("|") if c.strip()!=""]
        if len(cells) < 2: continue
        while len(cells) < len(hdrs): cells.append("")
        row = { hdrs[i]: cells[i] for i in range(min(len(hdrs), len(cells))) }
        role = _norm(row.get("role",""))
        if not role or role in {"role","roles"}: continue
        chosen = (row.get("chosen") or row.get("candidate(s)_in_design") or "").strip().strip("`")
        tok = re.split(r"[\s,]+", chosen)[0] if chosen else ""
        if tok and not re.match(rf"^{_ID}$", tok): tok = ""
        out[role] = {
            "signal": tok,
            "width": (row.get("width") or "").strip(),
            "notes": (row.get("notes") or "").strip(),
            "raw": row,
        }
    return out
def _first_present(d: Dict[str, Dict[str,str]], keys: List[str]) -> Optional[str]:
    for k in keys:
        if k in d and d[k].get("signal"):
            return d[k]["signal"]
    return None

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

def _infer_portlist(design_text: str, dut: str) -> List[Tuple[str, str, str]]:
    txt = _strip_comments(design_text)
    mod = re.search(
        rf"\bmodule\s+{dut}\b(?:\s*#\s*\(.*?\)\s*)?\s*\((?P<header>.*?)\)\s*;(?P<body>.*?)(?=\bendmodule\b)",
        txt, flags=re.S | re.I
    )
    if not mod: return []
    header = (mod.group("header") or "").strip()
    body   = (mod.group("body") or "")

    items: List[Tuple[str, str, str]] = []
    for line in re.split(r";|\n", header):
        if not re.search(r"\b(input|output|inout)\b", line): continue
        m = re.search(r"\b(input|output|inout)\b\s+(?:\w+\s+)*(\[[^]]+\])?\s*(?P<names>.+)$", line.strip())
        if not m: continue
        direction = m.group(1)
        width = (m.group(2) or "").strip()
        names_part = m.group("names")
        for raw in names_part.split(","):
            nm = re.split(r"=|\[", raw.strip())[0].strip().rstrip("),")
            if re.match(rf"^{_ID}$", nm): items.append((direction, width, nm))
    if items:
        seen = set(); out=[]
        for it in items:
            key = (it[0], it[2])
            if key in seen: continue
            seen.add(key); out.append(it)
        return out

    raw_names = [re.split(r"\s|\)", n.strip())[0] for n in header.split(",")]
    header_names = [n for n in raw_names if re.match(rf"^{_ID}$", n)]
    decls = {}
    for decl in re.finditer(r"\b(input|output|inout)\b\s+(?:\w+\s+)*(\[[^]]+\])?\s*([^;]+);", body, flags=re.S | re.I):
        direction = decl.group(1)
        width = (decl.group(2) or "").strip()
        name_list = decl.group(3)
        for raw in name_list.split(","):
            nm = re.split(r"=|\[", raw.strip())[0].strip()
            if re.match(rf"^{_ID}$", nm):
                decls[nm] = (direction, width)
    items = []
    for nm in header_names:
        if nm in decls:
            direction, width = decls[nm]
            items.append((direction, width, nm))
        else:
            items.append(("input", "", nm))
    seen = set(); out=[]
    for it in items:
        key = (it[0], it[2])
        if key in seen: continue
        seen.add(key); out.append(it)
    return out

def _fallback_clk_rst_from_ports(ports: List[Tuple[str,str,str]]) -> Tuple[Optional[str], Optional[str]]:
    inputs = [name for (d, _, name) in ports if d == "input"]
    clk = None
    for name in ("iClk","clk_i","clk"):
        if name in inputs: clk = name; break
    if not clk:
        cands = [n for n in inputs if re.search(r"(?:^|_)clk(?:_|$)|clock", n, re.I)]
        clk = sorted(cands, key=len)[0] if cands else None
    rst = None
    for pref in ("iRstN","rst_n","rst_ni","rst_i","reset_n","resetn","reset","rst"):
        if pref in inputs: rst = pref; break
    if rst is None:
        cands = [n for n in inputs if re.search(r"(rst(_n|_ni|_i)?|reset(_n|n|_i)?)", n, re.I)]
        rst = sorted(cands, key=len)[0] if cands else None
    return clk, rst

def _deduce_reset_polarity(name: Optional[str], notes_blob: str) -> Optional[bool]:
    if re.search(r"\bactive[-\s]?low\b", notes_blob, re.I):  return True
    if re.search(r"\bactive[-\s]?high\b", notes_blob, re.I): return False
    if name:
        if re.search(r"(rst(_n|_ni|\b)|resetn|nreset|rstb|_b|iRstN)$", name, re.I): return True
        if re.search(r"(reset|rst|rst_i)$", name, re.I): return False
    return None
def _reset_disable_expr_hier(rst_sig: Optional[str], is_active_low: Optional[bool]) -> Optional[str]:
    if not rst_sig: return None
    if is_active_low is True:  return f"!u_dut.{rst_sig}"
    if is_active_low is False: return f"u_dut.{rst_sig}"
    return None

_ROUND_KEY_DISCRETE = re.compile(r"\boKeyRound(?P<idx>\d{2})\b")

def _find_discrete_round_keys(text: str) -> List[str]:
    found = set(m.group("idx") for m in _ROUND_KEY_DISCRETE.finditer(text))
    idxs = sorted(int(x) for x in found)
    return [f"oKeyRound{idx:02d}" for idx in idxs]

def _find_llm_round_array_from_roles(roles: Dict[str, Dict[str,str]], design_text: str) -> Optional[str]:
    preferred_role_names = [
        "round_key_schedule", "round_key_output", "key_schedule",
        "round_key", "rk", "o_round_key"
    ]
    for rn in preferred_role_names:
        if rn in roles and roles[rn].get("signal"):
            sig = roles[rn]["signal"]
            if _token_present(design_text, sig):
                return sig
    for _, info in roles.items():
        sig = (info or {}).get("signal")
        if not sig: continue
        if re.search(r"^(k_sch|rk|round_?key|key_?sched|key_?schedule)$", sig, re.I):
            if _token_present(design_text, sig):
                return sig
    return None

def _find_array_round_keys_ranked(text: str) -> Optional[str]:
    txt = _strip_comments(text)
    names: List[str] = []
    for m in re.finditer(r"\b(?:wire|reg|logic)\s*\[\s*127\s*:\s*0\s*\]\s*(?P<name>[A-Za-z_]\w*)\s*\[[^\]]+\]\s*;", txt):
        names.append(m.group("name"))
    for m in re.finditer(r"\b(output|input|inout)\b[^;]*\[\s*127\s*:\s*0\s*\][^;]*\b(?P<name>[A-Za-z_]\w*)\s*\[[^\]]+\]\s*;", txt):
        names.append(m.group("name"))
    for m in re.finditer(r"\b(?P<name>[A-Za-z_]\w*)\s*\[\s*\d+\s*\]\s*\[\s*127\s*:\s*0\s*\]", txt):
        names.append(m.group("name"))
    if not names: return None
    cand = list(dict.fromkeys(names))
    def score(n: str) -> int:
        sc = 0
        if re.search(r"k[_]?sch|round_?key|rk|key_?sched|key_?schedule", n, re.I): sc += 5
        if re.search(r"key", n, re.I): sc += 2
        if re.search(r"round", n, re.I): sc += 1
        if re.search(r"^(istate|state|temp|w|sbox|t|buf)$", n, re.I): sc -= 6
        return sc
    cand.sort(key=lambda n: (-score(n), len(n)))
    best = cand[0]
    if score(best) < 0: return None
    return best

def _find_input_key_name(design_plus_analysis: str, roles: Dict[str, Dict[str,str]]) -> Optional[str]:
    for candidate in [
        _first_present(roles, ["input_key","key","ikey","key_in","key_i"]),
        "iKey","key_in","key_i","key"
    ]:
        if candidate and _token_present(design_plus_analysis, candidate):
            return candidate
    return None

def _find_word_arrays(design_text: str, roles: Dict[str, Dict[str,str]]) -> Tuple[Optional[str], Optional[str]]:
    prev_role = _first_present(roles, ["current_round_key","prev_words","keyword","words_prev","current_word","current_key"])
    curr_role = _first_present(roles, ["next_round_key","keyw","words_next","next_word","next_key"])
    prev = prev_role if _token_present(design_text, prev_role or "") else None
    curr = curr_role if _token_present(design_text, curr_role or "") else None
    if prev and curr:
        return prev, curr

    txt = _strip_comments(design_text)
    arrs = set()
    for m in re.finditer(r"\b(?:wire|reg|logic)\s*\[\s*31\s*:\s*0\s*\]\s*(?P<name>[A-Za-z_]\w*)\s*\[\s*0\s*:\s*3\s*\]\s*;", txt):
        arrs.add(m.group("name"))
    for m in re.finditer(r"\b(?P<name>[A-Za-z_]\w*)\s*\[\s*[0-3]\s*\]\s*\[\s*31\s*:\s*0\s*\]", txt):
        arrs.add(m.group("name"))

    def score_prev(n: str) -> int:
        sc=0
        if re.search(r"keyword|key_prev|prev|wprev|kw", n, re.I): sc+=5
        if re.search(r"key", n, re.I): sc+=1
        if re.search(r"^(istate|state|temp|buf)$", n, re.I): sc-=5
        return sc
    def score_curr(n: str) -> int:
        sc=0
        if re.search(r"keyw|wcur|next|wnew", n, re.I): sc+=5
        if re.search(r"key|w", n, re.I): sc+=1
        if re.search(r"^(istate|state|temp|buf)$", n, re.I): sc-=5
        return sc

    if arrs:
        cand = list(arrs)
        cand.sort(key=lambda n:(-score_prev(n),len(n)))
        prev2 = cand[0]
        cand.sort(key=lambda n:(-score_curr(n),len(n)))
        curr2 = cand[0]
        if prev2 == curr2 and len(cand)>1:
            curr2 = cand[1]
        if prev is None: prev = prev2
        if curr is None: curr = curr2

    return prev, curr

def _find_ready_signal(design_text: str) -> Optional[str]:
    for name in ("oKeyRoundReady","key_ready","key_valid","round_ready","rk_ready","ready","Valid","valid"):
        if _token_present(design_text, name):
            return name
    txt = _strip_comments(design_text)
    for m in re.finditer(r"\boutput\b[^;]*\[\s*\d+\s*:\s*0\s*\][^;]*\b([A-Za-z_]\w*(Ready|Valid))\b", txt, flags=re.I):
        return m.group(1)
    return None

_WRAPPER_TMPL = """`include "{design_base}"
module {wrapper_top};
{decls}

  // DUT instance
  {dut} u_dut(
{conns}
  );

  // ---------- Assertions ----------
{sva}

endmodule
"""
def _decl_line(name: str) -> str:
    return f"  logic {name};"
def _conn_line(port: str, net: str) -> str:
    return f"    .{port}({net}),"

def _module_body(design_text: str, dut: str) -> str:
    txt = _strip_comments(design_text)
    m = re.search(
        rf"\bmodule\s+{dut}\b(?:\s*#\s*\(.*?\)\s*)?\s*\(.*?\)\s*;(.*?)(?=\bendmodule\b)",
        txt, flags=re.S | re.I
    )
    return m.group(1) if m else ""

def _extract_xor_assigns_from_body(body: str) -> List[Tuple[str, str]]:

    b = re.sub(r"\\\n", " ", body)      
    b = re.sub(r"\n", " ", b)
    b = re.sub(r"\s+", " ", b).strip()

    assigns: List[Tuple[str, str]] = []

    _IDX = r"\[[^]]+\]"
    _LHS = rf"[A-Za-z_]\w*(?:\s*{_IDX}\s*){{0,3}}"
    _RHS = r"[^;]*\^[^;]*?"

    pat_cont = re.compile(rf"\bassign\s+(?P<lhs>{_LHS})\s*=\s*(?P<rhs>{_RHS})\s*;", flags=re.I)
    pat_proc = re.compile(rf"(?P<lhs>{_LHS})\s*(?:<=|=)\s*(?P<rhs>{_RHS})\s*;", flags=re.I)

    def _clean_lhs(s: str) -> str:
        return re.sub(r"\s+", "", s)

    for m in pat_cont.finditer(b):
        assigns.append((_clean_lhs(m.group("lhs")), m.group("rhs").strip()))
    for m in pat_proc.finditer(b):
        assigns.append((_clean_lhs(m.group("lhs")), m.group("rhs").strip()))

    seen=set(); out=[]
    for a in assigns:
        if a in seen: continue
        seen.add(a); out.append(a)
    return out

def _base_name(sig: str) -> str:
    return re.split(r"\s*\[", sig, maxsplit=1)[0]

def _signals_from_roles(roles: Dict[str, Dict[str, str]]) -> set:
    sigs = set()
    for v in (roles or {}).values():
        s = (v or {}).get("signal")
        if s: sigs.add(s)
    return sigs

def _filter_assigns_cluster(assigns: List[Tuple[str, str]], roles: Dict[str, Dict[str, str]]) -> List[Tuple[str, str]]:
    if not assigns: return assigns

    role_sigs = _signals_from_roles(roles)
    role_bases = { _base_name(s) for s in role_sigs }
    role_bases |= {"curW", "preW", "word_rnd_out", "word_rnd", "keyw", "words_arr_out", "words_arr_in"}

    kept: List[Tuple[str, str]] = []
    bases: set[str] = set()

    for lhs, rhs in assigns:
        b = _base_name(lhs)
        if b in role_bases:
            kept.append((lhs, rhs))
            bases.add(b)

    if not kept:
        return assigns  

    changed = True
    while changed:
        changed = False
        for lhs, rhs in assigns:
            if (lhs, rhs) in kept: continue
            b = _base_name(lhs)
            if b in bases or any(re.search(rf"\b{re.escape(kb)}\b", rhs) for kb in bases):
                kept.append((lhs, rhs))
                bases.add(b)
                changed = True

    ord_map = {(l, r): i for i, (l, r) in enumerate(assigns)}
    kept.sort(key=lambda x: ord_map.get(x, 10**9))
    return kept

def _xor_assigns_to_predicate(assigns: List[Tuple[str, str]], hier_prefix: str = "u_dut.") -> Optional[str]:

    if not assigns: return None

    _SV_RESERVED = {"posedge", "negedge", "inside"}  

    def _pref_id(m: re.Match) -> str:
        tok = m.group(1)
        if tok in _SV_RESERVED:
            return tok
        if tok.startswith(hier_prefix):
            return tok
        if re.match(r"^\d+$", tok):
            return tok

        return f"{hier_prefix}{tok}"

    id_or_index = r"(?<!')\b([A-Za-z_]\w*(?:\[[^]]+\])?)\b"

    clauses = []
    for lhs, rhs in assigns:
        lhs_h = lhs if lhs.startswith(hier_prefix) else f"{hier_prefix}{lhs}"
        rhs_h = re.sub(id_or_index, _pref_id, rhs)
        clauses.append(f"({lhs_h} == {rhs_h})")

    return " && ".join(clauses)


def generate_sva_for_aes_keyexp(
    *, category: str, property_name: str,
    design_path: str, analysis_path: str
):
    design_text   = _read(design_path)
    analysis_text = _read(analysis_path)
    blob = design_text + "\n" + analysis_text

    dut = _dut_from_analysis(analysis_text) or _dut_from_design(design_text, design_path)
    if not dut: raise RuntimeError("Could not find a module header in the design.")
    ports = _infer_portlist(design_text, dut)
    roles = _roles_from_table(analysis_text)
    clk_sig = _first_present(roles, ["clk","clock"]) or None
    rst_sig = _first_present(roles, ["reset","rst","rst_i","irstn","iRstN","rst_n","reset_n","resetn"]) or None
    if not clk_sig or not rst_sig:
        aclk, arst = _fallback_clk_rst_from_ports(ports)
        clk_sig = clk_sig or aclk
        rst_sig = rst_sig or arst
    notes_blob = "\n".join((roles.get("reset",{}).get("notes",""),
                            roles.get("rst",{}).get("notes",""),
                            roles.get("rst_i",{}).get("notes",""),
                            analysis_text or ""))
    rst_is_low = _deduce_reset_polarity(rst_sig, notes_blob)
    if rst_is_low is None and rst_sig:
        rst_is_low = True if re.search(r"(rst(_n|_ni|\b)|resetn|nreset|rstb|_b|iRstN)$", rst_sig, re.I) else False
    hdr = ""
    if clk_sig:
        hdr = f"@(posedge u_dut.{clk_sig})"
        rd = _reset_disable_expr_hier(rst_sig, rst_is_low)
        if rd: hdr += f" disable iff ({rd})"

    dut_body = _module_body(design_text, dut)
    xor_assigns_all = _extract_xor_assigns_from_body(dut_body)
    xor_assigns = _filter_assigns_cluster(xor_assigns_all, roles)
    predicate = _xor_assigns_to_predicate(xor_assigns, hier_prefix="u_dut.")

    props: List[str] = []

    if predicate:
        sva_text = f"""property p_keyexp_xor_chain;
  {hdr if hdr else ""}
    ({predicate});
endproperty
assert property (p_keyexp_xor_chain);"""
    else:
        arr_name = _find_llm_round_array_from_roles(roles, design_text) or _find_array_round_keys_ranked(design_text)
        discrete = [] if arr_name else _find_discrete_round_keys(design_text)
        input_key = _find_input_key_name(blob, roles)
        prev_words, curr_words = _find_word_arrays(design_text, roles)
        ready_sig = _find_ready_signal(design_text)

        templates = _load_templates(category)
        tpl = _pick_template(templates, property_name)
        if not tpl or not tpl.get("sva_template"):
            logger.warning("AES template for KeyExpansion not found; emitting diagnostic comment.")
            props.append("// No recognizable XOR chain and no template found.")
            sva_text = "\n\n".join(props)
        else:
            ttext = tpl["sva_template"]

            def antecedent_for_round(i: int, curr_expr: str) -> str:
                if ready_sig:
                    if re.search(rf"\b{ready_sig}\s*\[", design_text):
                        return f"$rose(u_dut.{ready_sig}[{i}])"
                    return f"$rose(u_dut.{ready_sig})"
                return f"({curr_expr} != $past({curr_expr}))"

            if arr_name:
                upper = 10
                mNr = re.search(r"\bparameter\s+(?:int\s+)?(Nr|NR|AES_NR)\s*=\s*(\d+)\b", design_text)
                if mNr:
                    try: upper = int(mNr.group(2))
                    except: pass
                for i in range(0, upper+1):
                    curr = f"u_dut.{arr_name}[{i}]"
                    if prev_words:
                        prev128 = f"{{u_dut.{prev_words}[3], u_dut.{prev_words}[2], u_dut.{prev_words}[1], u_dut.{prev_words}[0]}}"
                    else:
                        prev128 = (f"u_dut.{input_key}" if (input_key and i == 0) else f"u_dut.{arr_name}[{i-1}]")
                    antecedent = antecedent_for_round(i, curr)
                    subs = {
                        "round": f"{i:02d}",
                        "hdr": (hdr if hdr else ""),
                        "antecedent": antecedent,
                        "curr": curr,
                        "prev": prev128,
                    }
                    props.append(ttext.format(**subs).strip())

            elif discrete:
                for idx, rk in enumerate(discrete):
                    curr = f"u_dut.{rk}"
                    if prev_words:
                        prev128 = f"{{u_dut.{prev_words}[3], u_dut.{prev_words}[2], u_dut.{prev_words}[1], u_dut.{prev_words}[0]}}"
                    else:
                        prev128 = (f"u_dut.{input_key}" if (input_key and idx == 0) else
                                   (f"u_dut.{discrete[idx-1]}" if idx > 0 else curr))
                    antecedent = antecedent_for_round(idx, curr)
                    subs = {
                        "round": f"{idx:02d}",
                        "hdr": (hdr if hdr else ""),
                        "antecedent": antecedent,
                        "curr": curr,
                        "prev": prev128,
                    }
                    props.append(ttext.format(**subs).strip())
            else:
                props.append("// No recognizable round key outputs (array or discrete) found in DUT.")

            sva_text = "\n\n".join(props)

    decls: List[str] = []
    conns: List[str] = []
    if clk_sig:
        decls.append(_decl_line(clk_sig)); conns.append(_conn_line(clk_sig, clk_sig))
    if rst_sig:
        decls.append(_decl_line(rst_sig)); conns.append(_conn_line(rst_sig, rst_sig))
    if conns: conns[-1] = conns[-1].rstrip(",")

    design_base = os.path.basename(_drive_agnostic(design_path))
    dut_name = dut
    wrapper_top = f"{dut_name}__{property_name}_wrapper"
    wrapper_sv  = _WRAPPER_TMPL.format(
        design_base=design_base,
        wrapper_top=wrapper_top,
        dut=dut_name,
        decls=("\n".join(decls) if decls else "  // no top-level drivers"),
        conns=("\n".join(conns) if conns else "    // no connections"),
        sva=sva_text.strip(),
    )

    design_stub = os.path.splitext(design_base)[0]
    sv_out  = os.path.join(GENERATED_DIR, f"{design_stub}__{property_name}_wrapper.sv")
    _write(sv_out, wrapper_sv)

    tcl_lines = [
        "clear -all",
        "",
        f"analyze -sv12 {os.path.basename(sv_out)}",
        f"elaborate -top {wrapper_top} -create_related_covers witness",
        "",
    ]
    if clk_sig: tcl_lines.append(f"clock {clk_sig}")
    else:       tcl_lines.append("clock -none")
    if rst_sig:
        if rst_is_low: tcl_lines.append(f"reset -expression !{rst_sig}")
        else:          tcl_lines.append(f"reset {rst_sig}")
    else:
        tcl_lines.append("reset -none")
    tcl_lines += ["", "prove -all", ""]
    tcl_out = os.path.join(GENERATED_DIR, f"{property_name}_algo.tcl")
    _write(tcl_out, "\n".join(tcl_lines))

    logger.info("Generated wrapper: %s", sv_out)
    logger.info("Generated tcl    : %s", tcl_out)
    logger.info("Clock in TCL     : %s", clk_sig or "<none>")
    logger.info("Reset in TCL     : %s%s", "!" if rst_is_low else "", rst_sig or "<none>")

    return sv_out, tcl_out, wrapper_top



