
import os, posixpath, pathlib, logging, stat, time
import paramiko

from config import get_remote_cfg

os.makedirs("./logs", exist_ok=True)
logging.basicConfig(level=logging.INFO, filename="./logs/remote_runner.log")

_DRAIN_CHUNK = 65536
_POLL_SLEEP = 0.1  

def _connect():
    cfg    = get_remote_cfg()
    host   = cfg.get("host")
    port   = int(cfg.get("port", 22))
    user   = cfg.get("username")
    auth   = cfg.get("auth", "password")
    pw_env = cfg.get("password_env", "REMOTE_PASS")
    keypth = cfg.get("private_key", "")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    if auth == "key" and keypth:
        pkey = paramiko.RSAKey.from_private_key_file(keypth)
        ssh.connect(host, port=port, username=user, pkey=pkey)
    else:
        password = os.environ.get(pw_env)
        if not password:
            raise RuntimeError(f"Password auth selected but env var {pw_env} is not set")
        ssh.connect(host, port=port, username=user, password=password)
    return ssh

def _resolve_remote_dir(ssh, remote_dir: str) -> str:
    """Expand ~ on the server into $HOME."""
    if remote_dir.startswith("~"):
        _, stdout, _ = ssh.exec_command("printf $HOME")
        home = stdout.read().decode().strip()
        if not home:
            raise RuntimeError("Could not resolve $HOME on remote server")
        if remote_dir == "~":
            return home
        if remote_dir.startswith("~/"):
            return posixpath.join(home, remote_dir[2:])
    return remote_dir

def _ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str):
    """Recursively mkdir the remote path; safe if already exists."""
    parts = [p for p in remote_dir.split("/") if p]
    cur = "" if remote_dir.startswith("/") else "."
    for part in parts:
        cur = posixpath.join(cur, part)
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)

def _write_remote_file(sftp: paramiko.SFTPClient, remote_path: str, text: str):
    with sftp.file(remote_path, "w") as f:
        f.write(text)
    sftp.chmod(remote_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

def _drain(stdout, stderr):
    ch = stdout.channel
    while ch.recv_ready():
        ch.recv(_DRAIN_CHUNK)
    while ch.recv_stderr_ready():
        ch.recv_stderr(_DRAIN_CHUNK)

def _wait_for_exit(stdout, stderr, timeout_sec=None):
    ch = stdout.channel
    start = time.time()
    while True:
        if ch.exit_status_ready():
            _drain(stdout, stderr)
            return ch.recv_exit_status()
        _drain(stdout, stderr)
        if timeout_sec is not None and (time.time() - start) > timeout_sec:
            raise TimeoutError("Remote command timed out.")
        time.sleep(_POLL_SLEEP)

def run_verification(sva_path: str, tcl_path: str, design_file: str):
    
    #upload DUT 
    # + SVA/TCL, 
    # +run JG remotely, 
    # then it download the log

    cfg = get_remote_cfg()

    shell  = (cfg.get("shell", "bash") or "bash").lower()
    setup_cmd  = (cfg.get("setup_cmd", "") or "").strip()
    remote_dir_cfg= cfg["remote_working_directory"]
    log_name = cfg.get("log_path", "jg.log")

    local_sva = str(pathlib.Path(sva_path))
    local_tcl = str(pathlib.Path(tcl_path))
    local_dut = str(pathlib.Path(design_file))
    tcl_base  = pathlib.Path(local_tcl).name
    ssh = _connect()
    sftp = ssh.open_sftp()
    try:
        remote_dir = _resolve_remote_dir(ssh, remote_dir_cfg)
        _ensure_remote_dir(sftp, remote_dir)
        for p in (local_dut, local_sva, local_tcl):
            rdst = posixpath.join(remote_dir, pathlib.Path(p).name)
            logging.info(f"Uploading {p} -> {rdst}")
            sftp.put(p, rdst)


        if shell == "tcsh":
            runner = f"""#!/bin/tcsh
onintr -
{setup_cmd}
rehash
echo "SHELL: $SHELL"
echo "PATH: $PATH"
echo "which jg: `which jg >& /dev/null; if ($status) echo NONE; else which jg; endif`"
echo "which jaspergold: `which jaspergold >& /dev/null; if ($status) echo NONE; else which jaspergold; endif`"

cd "{remote_dir}"
echo "PWD: `pwd`"
ls -l

set tool=""
which jg >& /dev/null
if (! $status) set tool="jg"
if ("$tool" == "") then
  which jaspergold >& /dev/null
  if (! $status) set tool="jaspergold"
endif

if ("$tool" == "") then
  echo "ERROR: Neither jg nor jaspergold found on PATH."
  exit 127
endif

if ("$tool" == "jaspergold") then
  set cmd="$tool -allow_unsupported_OS -batch -tcl \"{tcl_base}\""
else
  set cmd="$tool -allow_unsupported_OS -batch \"{tcl_base}\""
endif

echo "RUN: $cmd"
eval $cmd |& tee "{log_name}"
"""
            run_cmd = f'tcsh "{posixpath.join(remote_dir, "run_jg.sh")}"'
        else:
            runner = f"""#!/usr/bin/env bash
set -euo pipefail
{setup_cmd}
echo "SHELL: $SHELL"
echo "PATH: $PATH"
echo "which jg: $(command -v jg || echo NONE)"
echo "which jaspergold: $(command -v jaspergold || echo NONE)"

cd "{remote_dir}"
echo "PWD: $(pwd)"
ls -l

tool="$(command -v jg || true)"
if [[ -z "$tool" ]]; then tool="$(command -v jaspergold || true)"; fi
if [[ -z "$tool" ]]; then
  echo "ERROR: Neither jg nor jaspergold found on PATH."
  exit 127
fi

if [[ "$(basename "$tool")" == "jaspergold" ]]; then
  cmd="$tool -allow_unsupported_OS -batch -tcl \"{tcl_base}\""
else
  cmd="$tool -allow_unsupported_OS -batch \"{tcl_base}\""
fi

echo "RUN: $cmd"
bash -lc "$cmd" > "{log_name}" 2>&1
"""
            run_cmd = f'bash "{posixpath.join(remote_dir, "run_jg.sh")}"'

        remote_runner = posixpath.join(remote_dir, "run_jg.sh")
        _write_remote_file(sftp, remote_runner, runner)

        logging.info(f"Executing: {run_cmd}")
        _, stdout, stderr = ssh.exec_command(run_cmd, get_pty=False)
        rc = _wait_for_exit(stdout, stderr, timeout_sec=None)
        logging.info(f"Remote jg exit: {rc}")

        remote_log = posixpath.join(remote_dir, log_name)
        local_log  = f"./logs/jaspergold_{tcl_base.rsplit('.', 1)[0]}.log"
        sftp.get(remote_log, local_log)
        logging.info(f"Downloaded log -> {local_log}")

        return local_log, rc
    finally:
        try:
            stdout.channel.close()
        except Exception:
            pass
        try:
            stderr.channel.close()
        except Exception:
            pass
        try:
            sftp.close()
        except Exception:
            pass
        ssh.close()
