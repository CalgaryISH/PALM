#design_analyzer agent

from openai import OpenAI

import logging, os
from config import resolve_llm

logging.basicConfig(level=logging.INFO, filename='./logs/design_analyzer.log')

def _supports_temperature(model: str) -> bool:
    
    # found thet some models don't support arbitrary temperature
    
    ml = (model or "").lower()
    return not (ml.startswith("o1") or ml.startswith("o3") or ml.startswith("gpt-5"))

def analyze_design(design_file, property, llm_choice='default'):
    cfg= resolve_llm(llm_choice)
    model= cfg['model']
    api_key= cfg.get('api_key') or os.environ.get('OPENAI_API_KEY')
    base_url= cfg.get('api_base')

    client = OpenAI(api_key=api_key, base_url=base_url)

    with open(design_file, "r", encoding="utf-8", errors="ignore") as f:
        design_content = f.read()

    name  = property.get('name', '')
    desc = property.get('description', '')
    extra  = property.get('extra', '')
    example_sva = property.get('example_sva', '')
    roles= property.get('roles', {})
    matchers = property.get('matchers', {})

    prompt = f"""

You are a formal analysis assistant. Read the design and locate the MOST relevant module and
signals for the target property.

PROPERTY
--------
Name: {name}
Description: {desc}
Extra guidance (hints):
{extra}
Example SVA (names are placeholders; do NOT copy blindly):
{example_sva}
Canonical roles to map (role → typical meaning):
{roles}
Name matchers (regex-ish hints):
{matchers}
DESIGN (verbatim)
-----------------
{design_content}

TASK
----
1) Pick the best module implementing the property behavior (give its name and a short justification).
2) Produce a role→signal mapping table with widths, e.g. (plain text is fine):
   | role | candidate(s) in design | chosen | width | notes |
3) List any assumptions needed (e.g., input stability between start/done).
4) Paste a minimal code snippet around the chosen signals (for context).
Return plain text (no JSON).
 
"""

    params = dict(model=model, messages=[{"role": "user", "content": prompt}])
    if _supports_temperature(model):
        params["temperature"] = 0.2
    resp = client.chat.completions.create(**params)

    analysis_result = resp.choices[0].message.content

    os.makedirs('./logs', exist_ok=True)
    out = f"./logs/{name or 'property'}_analysis.txt"
    with open(out, "w", encoding="utf-8", errors="replace") as f:
        f.write(analysis_result or "")
    return out

#You are a formal analysis assistant. Read the design and 
