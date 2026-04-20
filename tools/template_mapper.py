import json, re
from typing import Dict, List, Optional

def _best_match(signal_map: Dict[str,dict], matchers: List[str]) -> Optional[str]:
    chosen_names = [v.get("signal","") for v in signal_map.values() if v.get("signal")]
    for m in matchers or []:
        for s in chosen_names:
            if s.lower() == m.lower():
                return s
    for m in matchers or []:
        rx = re.compile(m, re.I)
        for s in chosen_names:
            if rx.search(s):
                return s
    return chosen_names[0] if chosen_names else None

def decide_roles(role_requirements: List[dict], llm_roles_json_path: str) -> Dict[str,str]:
    sig_map = json.load(open(llm_roles_json_path, "r", encoding="utf-8"))
    out: Dict[str,str] = {}
    for rr in role_requirements or []:
        rid  = rr.get("id")
        mats = rr.get("matchers") or []
        chosen = _best_match(sig_map, mats)
        if chosen:
           
            out[rid] = chosen
        elif rr.get("required", False):
                out[rid] = ""
    return out
