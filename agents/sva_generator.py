import os
import re
import json
import yaml
import time
import random
import logging
import pathlib
from typing import Optional, Dict, List, Tuple

import requests
from requests.exceptions import ReadTimeout, ConnectTimeout, ConnectionError

from tools.property_loader import load_properties

__all__ = ["generate_sva", "_resolve_llm_settings", "_llm_json"]

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
GENERATED_DIR = os.path.join(ROOT, "generated")
os.makedirs(GENERATED_DIR, exist_ok=True)

def _load_config() -> dict:
    candidates = []
    if os.environ.get("CONFIG_PATH"):
        candidates.append(os.environ["CONFIG_PATH"])
    cur = os.getcwd()
    while True:
        candidates += [
            os.path.join(cur, "config.yaml"),
            os.path.join(cur, "config", "config.yaml"),
        ]
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

    cur = HERE
    while True:
        candidates += [
            os.path.join(cur, "config.yaml"),
            os.path.join(cur, "config", "config.yaml"),
        ]
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

    for p in candidates:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            logger.info("Loaded config from %s", p)
            return cfg

    logger.warning("No config.yaml found; using empty config.")
    return {}
def _pick_profile(llms: dict, llm_choice: Optional[str]) -> str:
    if not isinstance(llms, dict):
        return ""
    if llm_choice and isinstance(llms.get(llm_choice), dict):
        return llm_choice
    default_name = llms.get("default")
    if isinstance(default_name, str) and isinstance(llms.get(default_name), dict):
        return default_name
    for k, v in llms.items():
        if k == "default":
            continue
        if isinstance(v, dict):
            return k
    return ""

def _resolve_llm_settings(llm_choice: Optional[str] = None) -> Dict[str, str]:
    cfg = _load_config()
    llms = cfg.get("llms") or {}
    if not isinstance(llms, dict):
        raise RuntimeError(f"config.llms must be a mapping, got {type(llms).__name__}")

    profile_name = _pick_profile(llms, llm_choice)
    profile = llms.get(profile_name) or {}
    if not isinstance(profile, dict):
        raise RuntimeError(
            f"config.llms['{profile_name}'] must be a mapping (YAML object), "
            f"but is {type(profile).__name__}. Check indentation under llms."
        )

    api_key = os.environ.get("OPENAI_API_KEY") or profile.get("api_key")
    if not api_key and profile.get("api_key_env"):
        api_key = os.environ.get(str(profile["api_key_env"]).strip())

    model = os.environ.get("LLM_MODEL") or profile.get("model") or "gpt-4o-mini"
    provider = profile.get("provider") or profile_name or "openai"

    base_url = (
        os.environ.get("OPENAI_BASE_URL")
        or profile.get("base_url")
        or ( "https://api.openai.com/v1") 
        #or ("https://api.deepseek.com/v1" if "deepseek" in (provider or "").lower() else "https://api.openai.com/v1") ##later adding  other llms
    )
    endpoint = base_url.rstrip("/")
    if not re.search(r"/chat/completions/?$", endpoint, flags=re.I):
        endpoint = endpoint + "/chat/completions"

    masked = (api_key[:4] + "…" + api_key[-4:]) if api_key and len(api_key) > 12 else ("<present>" if api_key else "<missing>")
    logger.info("LLM profile=%s provider=%s model=%s api_key=%s", profile_name or "<none>", provider, model, masked)

    if not api_key:
        raise RuntimeError(
            "No LLM API key. Set it in config.yaml under llms.<profile>.api_key or api_key_env, "
            "or set OPENAI_API_KEY."
        )

    return {
        "api_key": api_key,
        "model": model,
        "endpoint": endpoint,
        "provider": provider,
        "profile_name": profile_name or "",
    }

def _model_is_reasoning_or_gpt5(model: str) -> bool:
    ml = (model or "").lower()
    return ml.startswith("o1") or ml.startswith("o3") or ml.startswith("gpt-5")

def _chat_complete(prompt: str, llm_settings: Dict[str, str]) -> dict:
    headers = {
        "Authorization": f"Bearer {llm_settings['api_key']}",
        "Content-Type": "application/json",
    }

    def _payload(json_mode: bool) -> dict:
        p = {
            "model": llm_settings["model"],
            "messages": [
                {"role": "system", "content": "You are a senior formal verification engineer. Reply in JSON only."},
                {"role": "user", "content": prompt},
            ],
        }
        if not _model_is_reasoning_or_gpt5(llm_settings["model"]):
            p["temperature"] = 0.0
            p["response_format"] = {"type": "json_object"} 
        return p

    READ_TIMEOUT = int(os.environ.get("LLM_READ_TIMEOUT_SECS", "600"))
    CONNECT_TIMEOUT= int(os.environ.get("LLM_CONNECT_TIMEOUT_SECS", "30"))
    MAX_RETRIES  = int(os.environ.get("LLM_MAX_RETRIES", "6"))
    BACKOFF_BASE = float(os.environ.get("LLM_BACKOFF_BASE_SECS", "1.5"))

    json_mode = not _model_is_reasoning_or_gpt5(llm_settings["model"])
    last_err  = None
    last_text = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                llm_settings["endpoint"],
                headers=headers,
                json=_payload(json_mode),
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            code = resp.status_code
            last_text = resp.text[:1000]

            if code == 200:
                return resp.json()

            if code in (400, 415) and json_mode:
                json_mode = False
                continue

            retryable = (code in (408, 409, 425, 429)) or (code is not None and code >= 500)
            if retryable:
                ra = resp.headers.get("Retry-After")
                try:
                    delay = float(ra) if ra else (BACKOFF_BASE ** attempt) + random.uniform(0.0, 0.5)
                except ValueError:
                    delay = (BACKOFF_BASE ** attempt) + random.uniform(0.0, 0.5)
                time.sleep(min(30.0, delay))
                continue

            raise RuntimeError(f"LLM HTTP {code}: {last_text}")

        except (ReadTimeout, ConnectTimeout, ConnectionError) as e:
            last_err = e
            delay = (BACKOFF_BASE ** attempt) + random.uniform(0.0, 0.5)
            time.sleep(min(30.0, delay))
            continue

    raise RuntimeError(f"LLM request failed after {MAX_RETRIES} retries. Last response: {last_text or last_err}")

def _llm_json(prompt: str, llm_settings: Dict[str, str]) -> dict:
    j= _chat_complete(prompt, llm_settings)
    try:
        content = j["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError(f"Malformed LLM response: {json.dumps(j)[:600]}")
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"LLM did not return valid JSON: {content[:600]} (err: {e})")

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()

def _write(path: str, text: str) -> None:
    pathlib.Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def _strip_fences(s: str) -> str:
    s=re.sub(r"(?s)```[\w+-]*\s*", "", s)
    s=s.replace("```", "")
    s =re.sub(r"(?s)~~~[\w+-]*\s*", "", s)
    s = s.replace("~~~", "")
    return s

def _has_property_block(sv: str) -> bool:
    return re.search(r"\bproperty\s+[A-Za-z_]\w*\s*;[\s\S]*?endproperty", sv, flags=re.IGNORECASE) is not None
def _has_assertion(sv: str) -> bool:
    return re.search(r"\bassert\s+property\s*\(", sv, flags=re.IGNORECASE) is not None
def _extract_property_bodies(sv: str) -> List[str]:
    return re.findall(r"property\s+[A-Za-z_]\w*\s*;([\s\S]*?)endproperty", sv, flags=re.IGNORECASE)

def _looks_tautology(body: str) -> bool:
    has_ident = re.search(r"\b([A-Za-z_]\w*)\b", body) is not None
    return (not has_ident) and re.search(r"\b(1'b1|1|true)\b", body, flags=re.IGNORECASE) is not None

def _validate_wrapper_sv(wrapper_sv_core: str) -> Tuple[bool, str]:
    if not _has_property_block(wrapper_sv_core) or not _has_assertion(wrapper_sv_core):
        return False, "missing 'property ... endproperty' and/or 'assert property(...)'"
    bodies = _extract_property_bodies(wrapper_sv_core)
    if not bodies:
        return False, "no property body found"
    if all(_looks_tautology(b) for b in bodies):
        return False, "tautological property (e.g., 1'b1)"
    endmods = list(re.finditer(r"\bendmodule\b", wrapper_sv_core, flags=re.IGNORECASE))
    asserts = list(re.finditer(r"\bassert\s+property\s*\(", wrapper_sv_core, flags=re.IGNORECASE))
    if endmods and asserts and asserts[-1].start() > endmods[-1].start():
        return False, "assertion appears after endmodule"
    return True, ""

_WRAPPER_JSON_SPEC = """
Return strictly this JSON object:
{
  "top_module": "<string: the wrapper module name>",
  "wrapper_sv": "<string: complete SystemVerilog text for the wrapper module only (no `include`)>",
  "notes": "<optional: short reasoning>"
}
"""

def _build_wrapper_prompt(
    design_filename: str,
    design_text: str,
    property_name: str,
    category: str,
    analysis_text: Optional[str] = None,
    property_meta: Optional[dict] = None,
) -> str:

    MAX_CHARS = int(str(os.environ.get("LLM_MAX_PROMPT_CHARS", "80000")).replace("_", ""))
    if len(design_text) > MAX_CHARS:
        head = design_text[: MAX_CHARS // 2]
        tail = design_text[-MAX_CHARS // 2 :]
        shown = head + "\n/* … (truncated) … */\n" + tail
    else:
        shown = design_text

    roles_block = json.dumps((property_meta or {}).get("roles", {}), indent=2)
    matchers_block = json.dumps((property_meta or {}).get("matchers", {}), indent=2)
    extra_block = (property_meta or {}).get("extra", "").strip()
    example_block = (property_meta or {}).get("example_sva", "").strip()
    desc_block = (property_meta or {}).get("description", "")

    analysis_block = ""
    if analysis_text:
        analysis_block = (
            "\n\nAnalysis hints (read-only; may be heuristic or LLM-derived):\n"
            "---------------------------------------------------------------\n"
            f"{analysis_text}\n"
            "---------------------------------------------------------------\n"
        )

    return f"""
You are producing a *formal verification wrapper* for property "{property_name}" in category "{category}".
Infer the correct DUT module and its ports purely from the provided SystemVerilog source. Do *not* assume
specific signal names; derive them from the design.

PROPERTY GUIDANCE
-----------------
Description:
{desc_block}

Extra (read carefully):
{extra_block}

Canonical roles to map (role -> real signal):
{roles_block}

Suggested name matchers:
{matchers_block}

Example SVA (names are placeholders; ADAPT to real signals):
{example_block}

STRICT REQUIREMENTS
-------------------
- The wrapper must declare wires/logic with correct widths/types and instantiate the correct DUT.
- The wrapper must contain **exactly one** SVA property block and **one** assertion labeled `_assert_1:`.
- Place that property and assertion **immediately before the closing `endmodule`** of the wrapper.
- The property must be **meaningful** for the requested behavior (NOT a tautology like `1'b1`).
- Prefer real clocks if present; otherwise remain clock-free. Use `disable iff (reset)` with correct polarity.
- Ensure the property BODY ends with a semicolon before `endproperty`.
- Avoid illegal event expressions (`@posedge <module>`) and avoid hierarchical refs to non-existent signals.
- Output JSON only (spec below). No code fences/backticks.
Rules:
- Use only signal names listed in the ROLE TABLE (and clock/reset hints).
- Do NOT invent helper signals. If you need a previous value, use $past(<signal>, 1).


{analysis_block}Design file name (read-only): {design_filename}

SystemVerilog design source (read-only):
---------------------------------------
{shown}
---------------------------------------

{_WRAPPER_JSON_SPEC}
"""

def _build_fixup_prompt(base_prompt: str, reason: str, previous_wrapper_sv: str) -> str:
    return (
        base_prompt
        + f"""

VALIDATION FEEDBACK (read carefully and correct):
- Your previous output failed validation because: {reason}.
- You MUST return JSON whose "wrapper_sv":
  * Includes **exactly one** 'property ... endproperty' block
  * Includes **one** '_assert_1: assert property (...)'
  * Both appear **immediately before** 'endmodule'
  * Property is **not** a tautology (no '1'b1' etc.)

Previous attempt (read-only):
-----------------------------
{previous_wrapper_sv}
-----------------------------
"""
    )

def generate_sva(
    *,
    property_name,
    category: str = "Generic",
    design_file=None,
    design_path=None,
    analysis_path=None,
    design_label=None,
    llm_choice=None,
    **kwargs,
):
    design_file = design_file or design_path or kwargs.get("design")
    if not design_file:
        raise ValueError("design_file/design_path is required")

    analysis_text = None
    if analysis_path:
        try:
            with open(analysis_path, "r", encoding="utf-8") as f:
                analysis_text = f.read()
        except Exception:
            analysis_text = None

    if not os.path.exists(design_file):
        raise FileNotFoundError(f"Design file not found: {design_file}")

    design_text = _read(design_file)
    design_base = os.path.basename(design_file)
    design_stub = os.path.splitext(design_base)[0]

    prop_meta = None
    try:
        props = load_properties(category)
        prop_meta = next((p for p in props if str(p.get("name", "")).lower() ==
                         str(property_name).lower()), None)
    except Exception:
        prop_meta = None

    base_prompt = _build_wrapper_prompt(
        design_base,
        design_text,
        property_name,
        category or "Generic",
        analysis_text=analysis_text,
        property_meta=prop_meta,
    )
    llm_settings = _resolve_llm_settings(llm_choice)

    max_tries = int(os.environ.get("GEN_SVA_MAX_TRIES", "3"))
    last_sv = ""
    reason = "initial attempt"
    for attempt in range(1, max_tries + 1):
        prompt = base_prompt if attempt == 1 else _build_fixup_prompt(base_prompt, reason, last_sv)
        out = _llm_json(prompt, llm_settings=llm_settings)

        wrapper_sv_core = _strip_fences(out.get("wrapper_sv") or "")
        top = out.get("top_module") or ""
        notes = (out.get("notes") or "").strip()

        wrapper_sv_core = re.sub(
            r"(property\s+[a-zA-Z_]\w*\s*;\s*)([^;]*?)(\s*endproperty)",
            lambda m: m.group(1)
            + (m.group(2).rstrip() + ";" if m.group(2).strip() and not m.group(2).strip().endswith(";") else m.group(2))
            + m.group(3),
            wrapper_sv_core,
            flags=re.DOTALL,
        )

        ok, reason = _validate_wrapper_sv(wrapper_sv_core)
        if ok:
            if not top:
                m = re.search(r"\bmodule\s+([A-Za-z_]\w*)\s*\(", wrapper_sv_core)
                if not m:
                    reason = "no 'top_module' and cannot infer module name from wrapper_sv"
                    last_sv = wrapper_sv_core
                    continue
                top = m.group(1)

            full_wrapper_text = f'`include "{design_base}"\n\n{wrapper_sv_core.strip()}\n'
            if notes:
                full_wrapper_text += f"\n/* notes: {notes[:500]} */\n"

            wrapper_name = f"{design_stub}__with_wrapper__{property_name}.sv"
            wrapper_path = os.path.join(GENERATED_DIR, wrapper_name)
            _write(wrapper_path, full_wrapper_text)

            tcl_name = f"{property_name}.tcl"
            tcl_path = os.path.join(GENERATED_DIR, tcl_name)
            tcl = f"""clear -all

analyze -sv12 {os.path.basename(wrapper_path)}

elaborate -top {top} -create_related_covers witness

clock -none
reset -none

prove -all
"""
            _write(tcl_path, tcl)

            logger.info("Wrapper written: %s (top=%s)", wrapper_path, top)
            logger.info("TCL written    : %s", tcl_path)
            return wrapper_path, tcl_path, top

        last_sv = wrapper_sv_core or (out.get("wrapper_sv") or "")
        dbg = os.path.join(GENERATED_DIR, f"{design_stub}__with_wrapper__{property_name}__attempt{attempt}_invalid.sv")
        if last_sv:
            _write(dbg, last_sv)

    raise RuntimeError(f"LLM output invalid after {max_tries} tries: {reason}")
