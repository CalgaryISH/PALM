# from __future__ import annotations
# import time, pathlib, yaml
# from typing import Any, Dict, List, Optional, Tuple
# from openai import OpenAI
# from openai.types.chat import ChatCompletion
# try:
#     from tools.telemetry import LLMRunLogger   
# except Exception:
#     class LLMRunLogger:
#         def __init__(self, *_args, **_kwargs): pass
#         def write(self, _rec): pass

# _CFG_CACHE: Optional[dict] =  None

# def _read_yaml(path: str)  -> dict:
#     p = pathlib.Path(path)
#     if not p.exists():
#         return {}
#     return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
# def _cfg() -> dict:
#     global _CFG_CACHE
#     if _CFG_CACHE is None:
#         _CFG_CACHE = _read_yaml("config.yaml")
#     return _CFG_CACHE

# def _norm_keys(d: dict) -> dict:
#     return {str(k).replace("-", "_"): v for k, v in (d or {}).items()}

# def _get_llm_cfg(choice: str) -> dict:

#     cfg = _cfg()
#     llms = _norm_keys(cfg.get("llms") or {})
#     if not llms:
#         raise KeyError("No 'llms' section in config.yaml")

#     if choice in ("default", "", None):
#         choice = _norm_keys(llms).get("default") or "default"

#     key = choice.replace("-", "_")
#     if key in llms:
#         entry= llms[key] or {}
#     else:
#         entry = None
#         for v in llms.values():
#             if isinstance(v, dict) and str(v.get("model","")).strip() == choice:
#                 entry = v
#                 break
#         if entry is None:
#             raise KeyError(f"LLM choice '{choice}' not found in config.yaml (keys={list(llms.keys())}).")

#     provider = (entry.get("provider") or "openai").strip()
#     api_key  = (entry.get("api_key")  or "").strip()
#     model    = (entry.get("model")    or "").strip()
#     if not api_key:
#         import os
#         api_key = os.environ.get("OPENAI_API_KEY", "").strip()
#     if not api_key:
#         raise ValueError(f"Missing  api_key for LLM choice '{choice}'. "
#                          "Provide config.llms.<choice>.api_key or set OPENAI_API_KEY.")
#     if not model:
#         raise ValueError(f"Missing model for LLM choice '{choice}' in config.yaml.")
#     return {"provider": provider, "api_key": api_key, "model": model}

# def make_openai_client(choice: str) -> Tuple[OpenAI, str]:
#     llm = _get_llm_cfg(choice)
#     if llm["provider"].lower() != "openai":
#         raise ValueError(f"Only provider 'openai' is supported here, got '{llm['provider']}'.")
#     client = OpenAI(api_key=llm["api_key"])
#     return client, llm["model"]
# def call_openai_chat(*, messages: List[Dict[str, str]], choice: str,
#                      temperature: float = 0.0, max_tokens: int = 1600) -> str:
#     client, model = make_openai_client(choice)
#     resp: ChatCompletion = client.chat.completions.create(
#         model=model,
#         messages=messages,
#         temperature=temperature,
#         max_tokens=max_tokens,
#         stream=False,
#     )
#     return resp.choices[0].message.content or ""

# def call_openai_chat_logged(
#     *,
#     messages:List[Dict[str, str]],
#     choice:str,
#     role:str,                         
#     on_finish,                        
#     meta: Optional[Dict[str, Any]] = None,
#     temperature: float = 0.0,
#     max_tokens: int = 1600,
# ) -> str:
#     client, model = make_openai_client(choice)

#     start_ns=time.time_ns()
#     status, err = "ok", None
#     usage =None
#     resp_id =None
#     content = ""

#     try:
#         resp: ChatCompletion = client.chat.completions.create(
#                 model=model,
#                 messages=messages,
#                 temperature=temperature,
#                 max_tokens=max_tokens,
#                 stream=False,
#         )
#         resp_id = getattr(resp, "id", None)
#         usage = getattr(resp, "usage", None)
#         content = (resp.choices[0].message.content or "")
#         return content
#     except Exception as e:
#         status = "error"
#         err = repr(e)
#         raise
#     finally:
#         end_ns = time.time_ns()
#         prompt_tokens     = getattr(usage, "prompt_tokens", 0) if usage else 0
#         completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
#         rec = {
#              "model":model,
#              "role":role,
#              "start_time_ns": start_ns,
#             "end_time_ns": end_ns,
#             "duration_s": (end_ns - start_ns) / 1e9,
#             "prompt_tokens": prompt_tokens,
#             "completion_tokens": completion_tokens,
#             "response_id": resp_id,
#             "status": status,
#              "error":err,
#         }
#         if meta:
#             rec["meta"] = dict(meta)
#         try:
#             on_finish(rec)
#         except Exception:
#             pass
