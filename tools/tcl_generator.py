
import logging
import os

logging.basicConfig(level=logging.INFO, filename='./logs/tcl_generator.log')

def generate_tcl(sva_file, design_file, property):
    tcl_template = f"""
analyze -sv {os.path.basename(design_file)}
analyze -sv {os.path.basename(sva_file)}
elaborate -top {property['top_module']}
clock {property['clock']}
reset {property['reset']}
prove -all
"""

    tcl_path = f"./generated/{property['name']}.tcl"
    with open(tcl_path, 'w') as file:
        file.write(tcl_template)

    logging.info(f"TCL file generated for {property['name']}")

    return tcl_path
