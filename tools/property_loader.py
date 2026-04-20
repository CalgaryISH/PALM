from pathlib import Path
import yaml
import logging

logging.basicConfig(level=logging.INFO, filename='./logs/property_loader.log')

def _read_text_fallback(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("utf-8", b"", 0, 1, f"Could not decode {path} with common encodings")

def _normalize(s: str) -> str:
    return (
        s.replace("\u201c", '"')
         .replace("\u201d", '"')
         .replace("\u2018", "'")
         .replace("\u2019", "'")
         .replace("\u00a0", " ")
    )

def load_properties(category: str):
    base = Path("data/properties") / category
    # Try .yml first (legacy), then .yaml
    path = None
    for cand in (base.with_suffix(".yml"), base.with_suffix(".yaml")):
        if cand.exists():
            path = cand
            break
    if path is None:
        raise FileNotFoundError(f"Properties file not found: {base}.yml or {base}.yaml")

    raw = _read_text_fallback(path)
    cleaned = _normalize(raw)
    props = yaml.safe_load(cleaned)
    if not isinstance(props, list):
        raise ValueError(f"{path} must be a YAML list of property dicts.")
    logging.info(f"Loaded {len(props)} properties for category {category} from {path}")
    return props



# from pathlib import Path
# import yaml
# import logging

# logging.basicConfig(level=logging.INFO, filename='./logs/property_loader.log')

# def _read_text_fallback(path: Path) -> str:
#     for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
#         try:
#             return path.read_text(encoding=enc)
#         except UnicodeDecodeError:
#             continue
#     raise UnicodeDecodeError("utf-8", b"", 0, 1, f"Could not decode {path} with common encodings")
