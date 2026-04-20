
import os
import json
from functools import lru_cache
from pathlib import Path
import yaml

def _expand_env(v):
    if isinstance(v, str):
        return os.path.expandvars(v)
    return v
def _find_config_path() -> Path:
    env = os.environ.get("CONFIG_PATH")
    if env:
        p = Path(env).expanduser()
        if p.exists():
            return p
    here = Path(__file__).resolve().parent
    cand = here / "config.yaml"
    if cand.exists():
        return cand
    cand2 = here.parent / "config.yaml"
    if cand2.exists():
        return cand2
    raise FileNotFoundError(
        f"config.yaml not found. Tried {cand} and {cand2}. "
        "Set CONFIG_PATH env var"
    )

@lru_cache(maxsize=1)
def _load_config() -> dict:
    cfg_path = _find_config_path()
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "llms" in data and isinstance(data["llms"], dict):
        for k, v in list(data["llms"].items()):
            if isinstance(v, dict):
                data["llms"][k] = {kk: _expand_env(vv) for kk, vv in v.items()}
            else:
                data["llms"][k] = _expand_env(v)
    if "remote_execution" in data and isinstance(data["remote_execution"], dict):
        data["remote_execution"] = {k: _expand_env(v) for k, v in data["remote_execution"].items()}
    return data

def get_remote_cfg() -> dict:
    return _load_config().get("remote_execution", {})

def resolve_llm(choice: str):
    cfg = _load_config()
    llms = cfg.get("llms", {})

    def expand_profile(v):
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            ref = llms.get(v)
            if ref is not None and ref is not v:
                return expand_profile(ref)
            return {"model": v}
        return {}

    base = llms.get(choice)
    if base is None:
        default_key_or_model = llms.get("default")
        if default_key_or_model is not None:
            base = default_key_or_model
        else:
            base = choice

    prof = expand_profile(base)

    model    = _expand_env(prof.get("model", "")) if isinstance(prof, dict) else ""
    api_key  = _expand_env(prof.get("api_key", "")) if isinstance(prof, dict) else ""
    api_key  = api_key or os.environ.get("OPENAI_API_KEY")
    api_base = _expand_env(prof.get("api_base", "")) if isinstance(prof, dict) else ""
    api_base = api_base or None

    if not model:
        if isinstance(choice, str) and choice.strip():
            model = choice.strip()
        else:
            raise ValueError(
                f"Unable to resolve LLM model for choice '{choice}'. "
                f"Check config.yaml llms section."
            )

    return {"model": model, "api_key": api_key, "api_base": api_base}


