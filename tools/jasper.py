import subprocess, shlex, pathlib, re


def run_jasper(tcl_path:str, work_dir:str=None, timeout:int=900):
    cmd = f"jg {shlex.quote(tcl_path)}"
    proc = subprocess.run(cmd, shell=True,cwd=work_dir,capture_output=True,text=True, timeout=timeout)
    return proc.returncode, proc.stdout + "\n" + proc.stderr


def parse_jg_log(log:str):
    status = "PASS" if "Verification completed" in log and "failed" not in log.lower() else "FAIL"
    falsified = re.findall(r"Property\s+(\w+)\s+failed", log)
    return {"status":status, "failed":falsified}
