import argparse, subprocess
from pathlib import Path

def main():
    ap = argparse.ArgumentParser(description="Batch: final_roles_*.xlsx to  analysis text files per sheet")
    ap.add_argument("--family",default="AES")
    ap.add_argument("--roles_dir",default="static_mod/AES", help="Folder with final_roles_*.xlsx")
    ap.add_argument("--out_root",  default="generated/AES",help="root folder for output analysis dirs")
    args = ap.parse_args()
    roles_dir = Path(args.roles_dir)
    for xl in roles_dir.glob("final_roles_*.xlsx"):
        label = xl.stem.replace("final_roles_", "")
        outdir = Path(args.out_root) / f"analysis_{label}"
        outdir.mkdir(parents=True, exist_ok=True)

        print(f"[RUN] {xl} -> {outdir}")
        subprocess.run([
            "python", "-m", "agents.make_analysis_from_roles",
            "--family", args.family,
            "--final_roles", str(xl),
            "--outdir", str(outdir),
        ], check=True)

if __name__ == "__main__":
    main()

