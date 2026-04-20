# PALM Pipeline
SVA generation and formal verification for RTL designs across different design families.

---


For any variant that uses an LLM, add your API key to `config.yaml`:
```
llms:
  default:
    model: 
    api_key: 
```

---

## Step 1 — PDG Extraction
```
python agents/PDG.py --family AES \
  --design_folder rtl_json/AES \
  --out_dir static_var_new/AES \
  --pdg_json_dir static_pdg/AES
```

---

## Step 2 — Pairwise INT Workbooks
```
python run_pipeline.py --family AES \
  --design1_json "rtl_json/AES" \
  --design2_json "rtl_json/AES/AES-T100.json" \
  --design2_excel "static_var_new/AES/AES-T100.xlsx" \
  --out_dir "static_pairs/AES" \
  --design1_excel_dir "static_var_new/AES"
```
Assistant names: AES → `AES-T100`.

---

## Step 3 — Role Selection
```
# AES — Thr-based 
python agents/aes_pairs_prune.py \
  --int_dir static_pairs/AES \
  --design1_excel_dir static_var_new/AES \
  --out_dir static_mod_new/AES \
  --functions "AddRoundKey,SBox,ShiftRows,KeyExpansion" --no_llm
```


---

## Step 4 — Convert Roles to .txt  
```
python agents/make_analysis_from_roles.py \
  --family AES \
  --final_roles static_mod_new/AES/final_roles_AES_SV_01.xlsx \
  --outdir generated/AES/analysis_AES_SV_01
```
Repeat for each design. Replace `AES` / `AES_SV_01` with the target family and label.


---

## Step 5 — Run the Pipeline

Four variants combine `--analysis_mode` (`llm` or `static`) with `--generation_mode` (`llm` or `algo`).
```
## AES pa-pa: no API key
python -m batch_runner \
  --categories AES \
  --design_glob "designs/AES/*.sv" \
  --properties "AddRoundKey,SBox,ShiftRows,KeyExpansion" \
  --analysis_mode static \
  --generation_mode algo \
  --analysis_root "generated/AES" \
  --out_csv "logs/AES_pa_pa_{ts}.csv"

```
Single design:
```
python cli.py --category AES \
  --design designs/AES/AES_SV_01.sv \
  --property AddRoundKey \
  --analysis_mode static \
  --generation_mode algo \
  --analysis_path generated/AES/analysis_AES_SV_01/AddRoundKey_analysis.txt
```
