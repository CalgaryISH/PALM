import argparse, glob, os, csv, time, pathlib, sys
from typing import List
from agents.coordinator import run_agentic_pipeline
from tools.property_loader import load_properties

def _split_csv(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]

def _resolve_properties(category: str, arg: str) -> List[str]:
    if arg in ("*", "all", "ALL"):
        return [p["name"] for p in load_properties(category)]
    return _split_csv(arg)

def _list_designs(design_glob: str) -> List[str]:
    files = sorted(glob.glob(design_glob))
    return [f for f in files if os.path.isfile(f)]

def main():
    ap = argparse.ArgumentParser(
        description="Batch runner for the agentic SVA pipeline (categories × designs × properties).")
    ap.add_argument("--categories",default="AES", help="Comma list,ex: AES,RSA,SHA (default: AES)")
    ap.add_argument("--design_glob",default="designs/{category}/*.sv",help="Ex: 'designs/{category}/*.sv'")
    ap.add_argument("--properties", default="*", help="Comma list of property names, or '*' to use all from that category's YAML.")
    ap.add_argument("--analysis_mode", choices=["llm","algo","static"], default="llm", help="Use 'algo' here for Algo+Algo.")
    ap.add_argument("--generation_mode",choices=["algo","llm"], default="llm",help="Use 'algo' here for Algo+Algo.")
    ap.add_argument("--analysis_root", default="generated/{category}",help="Root folder where per-design analysis_* folders live. E.g. generated/{category}")
    ap.add_argument("--pdg_excel_dir", default="static_var_new/{category}", help="Folder with per-design PDG Excel files for static analysis mode")
    ap.add_argument("--function_name", default=None,  help="Sheet override: fsm_module / top_module etc.")

    ap.add_argument("--llm_choice",default="default")
    ap.add_argument("--out_csv", default="logs/batch_results_{ts}.csv")
    args = ap.parse_args()

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_csv = args.out_csv.replace("{ts}", ts)
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)

    cats = _split_csv(args.categories)
    if not cats:
        print("No categories provided.", file=sys.stderr); sys.exit(2)

    rows = []
    for cat in cats:
        
        try:
            props = _resolve_properties(cat, args.properties)
        except FileNotFoundError:
            print(f"[WARN] No properties YAML for category {cat}; skipping.", file=sys.stderr)
            continue
        if not props:
            print(f"[WARN] No properties for category {cat}; skipping.", file=sys.stderr)
            continue

    
        pattern = args.design_glob.format(category=cat)
        designs = _list_designs(pattern)
        if not designs:
            print(f"[WARN] No designs matched: {pattern}", file=sys.stderr)
            continue

        analysis_root = pathlib.Path(args.analysis_root.format(category=cat))

        for design_path in designs:
            design_name = os.path.basename(design_path)              #AES_SV_01.sv
            label = pathlib.Path(design_path).stem                   #AES_SV_01
            per_design_analysis_dir = analysis_root / f"analysis_{label}"
            for prop in props:
                analysis_path = per_design_analysis_dir / f"{prop}_analysis.txt"
                if args.analysis_mode == "algo" and args.generation_mode == "algo":
                    if not analysis_path.exists():
                        print(f"[SKIP] {cat} | {design_name} | {prop} : missing analysis file {analysis_path}", file=sys.stderr)
                        continue

                
                print(f"==> {cat} | {design_name} | {prop} | modes={args.analysis_mode}/{args.generation_mode}", flush=True)
                
                
                pdg_excel_dir = pathlib.Path(args.pdg_excel_dir.format(category=cat))
                fn_default = {"FSM": "fsm_module", "RSA": "top_module", "SHA": "top_module"}.get(cat, prop)
                fn = args.function_name or fn_default

                try:
                    res = run_agentic_pipeline(
                        category=cat,
                        design_file=design_path,
                        property_name=prop,
                        llm_choice=args.llm_choice,
                        analysis_mode=args.analysis_mode,
                        generation_mode=args.generation_mode,
                        analysis_path=(str(analysis_path) if analysis_path.exists() else None),
                        pdg_excel=str(pdg_excel_dir / f"{label}.xlsx"),
                        function_name=fn,
                    )
                # try:
                #     res = run_agentic_pipeline(
                #         category=cat,
                #         design_file=design_path,
                #         property_name=prop,
                #         llm_choice=args.llm_choice,
                #         analysis_mode=args.analysis_mode,
                #         generation_mode=args.generation_mode,
                #         analysis_path=(str(analysis_path) if analysis_path.exists() else None),
                #     )
                    counts = res.get("counts", {}) if isinstance(res, dict) else {}
                    rows.append([
                            cat,
                            design_name,
                            prop,
                            res.get("result", res if isinstance(res, str) else "unknown"),
                            counts.get("considered", 0),
                            counts.get("proven", 0),
                            counts.get("covered", 0),
                            counts.get("errors", counts.get("error", 0)),
                            res.get("top", ""),
                            res.get("wrapper", ""),
                            res.get("tcl", ""),
                    ])
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    print(f"[ERROR] {cat} | {design_name} | {prop} → {err}", file=sys.stderr, flush=True)
                    rows.append([
                        cat,
                        design_name,
                        prop,
                        "error",
                        0, 0, 0, 0,
                        "", "", ""
                    ])

    #writin csv summary
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["category","design","property","result","considered","proven","covered","errors","top","wrapper_sv","tcl"])
        w.writerows(rows)
    print(f"\nwrote summary:::::::::: {out_csv}")
    print("artifacts per run are under ./generated and ./logs.")

if __name__ == "__main__":
    main()
