
import argparse
from agents.coordinator import run_agentic_pipeline

def main():
    ap = argparse.ArgumentParser(description="Run agentic SVA pipeline")
    ap.add_argument('--category',required=True,help="IP family, ex: AES, RSA, SHA, FSM")
    ap.add_argument('--design',required=True,help="Path to DUT .sv")
    ap.add_argument('--property', required=True,help=("Property name,ex: AddRoundKey/SBox/ShiftRows/KeyExpansion "
                          "for (AES), RSA_In_Out_Diff for (RSA), Outputs_Diff for (SHA), always_legal_state for (FSM)"))
    ap.add_argument('--llm_choice', default='default',help="Model profile key from config.yaml")


    ap.add_argument('--analysis_mode', choices=['algo', 'llm', 'static'], default='llm', help=("")) 
    #########needs to edit


    ap.add_argument('--generation_mode',choices=['algo', 'llm'], default='llm', help="How to build wrapper+TCL: algo or llm")


    ap.add_argument('--pdg_excel', required=False,help="Path to PDG/static Excel ")
    ap.add_argument('--function_name', required=False, default=None, help=(
                          "AES: AddRoundKey/SBox/ShiftRows/KeyExpansion; "
                          "RSA: top_module; SHA: top_module; FSM: fsm_module. "))

    ap.add_argument('--summary_excel',required=False,help="Summary_<FAMILY>.xlsx that maps design json to the best module")

    ap.add_argument('--analysis_path', required=False, help="if provided, analysis step is skiped.")

    args = ap.parse_args()

    results = run_agentic_pipeline(
            design_file=args.design,
            category=args.category,
            property_name=args.property,
            llm_choice=args.llm_choice,
            analysis_mode=args.analysis_mode,
            generation_mode=args.generation_mode,
            pdg_excel=args.pdg_excel,
            function_name=args.function_name,
            analysis_path=args.analysis_path,
            summary_excel=args.summary_excel,
    )
    print(results)

if __name__ == '__main__':
    
    main()