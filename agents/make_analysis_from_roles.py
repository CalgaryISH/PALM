import argparse
from pathlib import Path
import pandas as pd
from textwrap import dedent
import re

HEADER = (
    "| role | candidate(s) in design | chosen | width | notes |\n"
    "| ---  | ---                     | ---    | ---   | ---   |"
)

def _sheet_to_table(df: pd.DataFrame) -> str:
    cols = {c.lower(): c for c in df.columns}
    role_c   = cols.get("role", "Role")
    chosen_c = cols.get("chosen", "Chosen")
    notes_c  = cols.get("notes", "Notes")

    lines = [HEADER]
    for _, r in df.iterrows():
        role   = str(r.get(role_c, "") or "")
        chosen = str(r.get(chosen_c, "") or "")
        notes  = str(r.get(notes_c, "") or "from pairs")
        lines.append(f"| {role} | - | `{chosen}` |  | {notes} |")
    return "\n".join(lines)

def _find_dut(df: pd.DataFrame) -> str:
    cols = {c.lower(): c for c in df.columns}
    role_c   = cols.get("role", "Role")
    chosen_c = cols.get("chosen", "Chosen")
    if role_c not in df.columns or chosen_c not in df.columns:
        return ""
    mask = df[role_c].astype(str).str.strip().str.lower().isin(["dut","module","top","top_module"])
    ddf = df[mask]
    if ddf.empty:
        return ""
    return str(ddf.iloc[0][chosen_c]).strip()

def main():
    ap = argparse.ArgumentParser(description="convert final_roles.xlsx to analysis .txt per sheet")
    ap.add_argument("--family",required=True,help="IP family name (ex AES)")
    ap.add_argument("--final_roles",required=True, help="path to final_roles_*.xlsx")
    ap.add_argument("--outdir",required=True,help="where to write analysis .txt files")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    xl = pd.ExcelFile(args.final_roles)
    for sheet in xl.sheet_names:
        df = pd.read_excel(args.final_roles, sheet_name=sheet)
        dut = _find_dut(df)
        best_mod = f"`{dut}`" if dut else "<n/a>"
        table = _sheet_to_table(df)
        body = dedent(f"""
        Best module for {args.family}:{sheet} (from final_roles) → {best_mod}

        ROLE TABLE
        ----------
        {table}

        NOTES
        -----
        This analysis file was created from pruned pairs (+ optional LLM) selections (final_roles.xlsx).
        Widths can be inferred by the generator from the design if omitted.
        """).strip()

        out_txt = outdir / f"{sheet}_analysis.txt"
        out_txt.write_text(body, encoding="utf-8")
        print(f"wrote {out_txt}")

if __name__ == "__main__":

    main()





