

import csv, json, os, re, pathlib
from typing import List, Dict

_ID = r"[A-Za-z_]\w*"

def _norm(s: str) -> str:
    return re.sub(r"\s+", "_", (s or "").strip().lower())

def parse_markdown_roles_table(analysis_text: str) -> List[Dict[str, str]]:
    lines = [ln for ln in (analysis_text or "").splitlines() if "|" in ln]
    if not lines:
        return []
    header_idx, headers = None, None
    for i, ln in enumerate(lines):
        cells = [c.strip() for c in ln.split("|") if c.strip() != ""]
        if any("role" in c.lower() for c in cells):
            header_idx, headers = i, cells
            break
    if header_idx is None:
        return []
    hdrs = [_norm(h) for h in headers]
    data = lines[header_idx+1:]
    if data and re.match(r"^\s*\|?\s*[:-]{2,}", data[0].replace("|","").strip()):
        data = data[1:]

    rows: List[Dict[str,str]] = []
    for ln in data:
        cells = [c.strip() for c in ln.split("|") if c.strip()!=""]
        if len(cells) < 2:
            continue
        while len(cells) < len(hdrs):
            cells.append("")
        row = { hdrs[i]: cells[i] for i in range(min(len(hdrs), len(cells))) }
        rows.append(row)
    return rows

def extract_mapping(rows: List[Dict[str,str]]) -> Dict[str, Dict[str,str]]:
    out: Dict[str, Dict[str,str]] = {}
    for r in rows:
        role = _norm(r.get("role",""))
        if not role:
            continue
        chosen = (r.get("chosen") or r.get("candidate(s)_in_design") or "").strip()
        tok = re.split(r"[\s,]+", chosen)[0] if chosen else ""
        if tok and not re.match(rf"^{_ID}$", tok):
            tok = ""
        out[role] = {
            "signal": tok,
            "width": (r.get("width") or "").strip(),
            "notes": (r.get("notes") or "").strip(),
            "raw": r,
        }
    return out

def write_csv_json(analysis_path: str, out_dir: str = "./generated") -> str:
    txt = pathlib.Path(analysis_path).read_text(encoding="utf-8", errors="ignore")
    rows = parse_markdown_roles_table(txt)
    mp   = extract_mapping(rows)

    base = pathlib.Path(analysis_path).stem
    pathlib.Path(out_dir).mkdir(parents=True, exist_ok=True)
    csv_path = os.path.join(out_dir, f"{base}_roles.csv")
    if rows:
        headers = sorted({k for r in rows for k in r.keys()})
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            w.writeheader()
            w.writerows(rows)
    json_path = os.path.join(out_dir, f"{base}_roles.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(mp, f, indent=2)
    return json_path
