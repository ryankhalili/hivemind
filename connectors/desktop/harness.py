"""Opt-in local process adapters. Executable registrations stay on this device."""
from __future__ import annotations

import json
import copy
import hashlib
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

OUTPUT_LIMIT = 16000


class ProcessGroup:
    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit",ctypes.c_longlong),("PerJobUserTimeLimit",ctypes.c_longlong),
                        ("LimitFlags",wintypes.DWORD),("MinimumWorkingSetSize",ctypes.c_size_t),("MaximumWorkingSetSize",ctypes.c_size_t),
                        ("ActiveProcessLimit",wintypes.DWORD),("Affinity",ctypes.c_size_t),("PriorityClass",wintypes.DWORD),("SchedulingClass",wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name,ctypes.c_ulonglong) for name in ("ReadOperationCount","WriteOperationCount","OtherOperationCount","ReadTransferCount","WriteTransferCount","OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation",Basic),("IoInfo",IO),("ProcessMemoryLimit",ctypes.c_size_t),
                        ("JobMemoryLimit",ctypes.c_size_t),("PeakProcessMemoryUsed",ctypes.c_size_t),("PeakJobMemoryUsed",ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32",use_last_error=True)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = self.kernel.CreateJobObjectW(None,None)
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        if handle and self.kernel.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits)) and self.kernel.AssignProcessToJobObject(handle,int(process._handle)):
            self.handle = handle
        elif handle:
            self.kernel.CloseHandle(handle)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Cancelled(Exception):
    pass


def stop_process(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000, timeout=15)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_process(argv, cwd, timeout, cancel, stdin=None, progress=lambda _: None):
    if cancel.is_set():
        raise Cancelled("Cancelled before execution.")
    environment = {key:value for key,value in os.environ.items() if key.upper() not in {
        "HIVE_TOKEN", "HIVE_NODE_TOKEN", "HIVEMIND_TOKEN", "HIVE_OWNER_PASSWORD", "HIVE_SERVER", "HIVE_BASE_URL"}}
    options = {"creationflags": 0x08000000 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(argv, cwd=str(cwd), shell=False, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, **options)
    group = ProcessGroup(process)
    output = {"stdout":bytearray(), "stderr":bytearray()}
    truncated = threading.Event()
    lock = threading.Lock()
    def collect(name, stream):
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            with lock:
                room = OUTPUT_LIMIT - sum(len(data) for data in output.values())
                output[name].extend(chunk[:max(0,room)])
                if len(chunk) > room:
                    truncated.set()
        stream.close()
    threads = [threading.Thread(target=collect,args=(name,getattr(process,name)),daemon=True) for name in output]
    for thread in threads:
        thread.start()
    def send_input():
        try:
            process.stdin.write(stdin)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    if stdin is not None:
        threading.Thread(target=send_input,daemon=True).start()
    start, last_progress = time.monotonic(), 0
    try:
        while process.poll() is None:
            elapsed = time.monotonic() - start
            if cancel.is_set():
                raise Cancelled("Stopped on this device. Earlier changes may remain.")
            if elapsed >= timeout:
                raise TimeoutError("Local process reached its time limit.")
            if elapsed - last_progress >= 5:
                progress(f"Local process is running ({int(elapsed)} seconds).")
                last_progress = elapsed
            cancel.wait(0.1)
        # An exited parent may have left descendants holding output pipes.
        for thread in threads:
            thread.join(timeout=0.5)
        if any(thread.is_alive() for thread in threads):
            group.close()
            stop_process(process)
            for thread in threads:
                thread.join(timeout=1)
        with lock:
            return {"exit_code":process.returncode,"stdout":output["stdout"].decode("utf-8",errors="replace"),
                    "stderr":output["stderr"].decode("utf-8",errors="replace"),"truncated":truncated.is_set()}
    except BaseException:
        group.close()
        stop_process(process)
        raise
    finally:
        group.close()


class Runner:
    def __init__(self, folder, commands=False, harnesses=None, confirm=None, remote_approval=False):
        self.folder, self.commands = folder, commands
        self.harnesses = copy.deepcopy(harnesses or {})
        for adapter in self.harnesses.values():
            fixed=[]
            for argument in adapter.get("arguments",[]):
                if not isinstance(argument,str) or "\0" in argument:
                    raise ValueError("Local adapter arguments must be strings.")
                candidate=Path(argument).expanduser()
                if not candidate.is_absolute() and (folder.root/candidate).is_file():
                    argument=str((folder.root/candidate).resolve())
                elif not candidate.is_absolute() and not argument.startswith("-") and candidate.suffix.lower() in {".py",".pyw",".js",".mjs",".sh"}:
                    raise ValueError("Choose an existing adapter script using an absolute path.")
                fixed.append(argument)
            adapter["arguments"]=fixed
        self.revisions = {key:self.fingerprint(value) for key,value in self.harnesses.items()}
        self.confirm = confirm or (lambda details,cancel:False)
        self.remote_approval = bool(remote_approval)

    def fingerprint(self, adapter):
        executable = Path(adapter["executable"]).expanduser().resolve(strict=True)
        config = {"executable":str(executable),"arguments":adapter.get("arguments",[]),"kind":adapter.get("kind","custom"),"protocol":2}
        digest = hashlib.sha256(json.dumps(config,sort_keys=True,separators=(",",":")).encode())
        paths = [executable]
        # Include explicitly configured script files, without exposing their paths.
        for argument in config["arguments"]:
            if not isinstance(argument,str) or "\0" in argument:
                raise ValueError("Local adapter arguments must be strings.")
            candidate=Path(argument).expanduser()
            if candidate.is_absolute() and candidate.is_file():
                paths.append(candidate.resolve())
            elif not candidate.is_absolute() and (self.folder.root/candidate).is_file():
                paths.append((self.folder.root/candidate).resolve())
        for path in paths:
            digest.update(str(path).encode())
            with path.open("rb") as source:
                for chunk in iter(lambda:source.read(1024*1024),b""):
                    digest.update(chunk)
        return digest.hexdigest()

    def check_revision(self, harness_id):
        adapter=self.harnesses.get(harness_id)
        if adapter is None:
            raise ValueError("Select a locally registered harness.")
        try:
            unchanged=self.fingerprint(adapter)==self.revisions[harness_id]
        except (OSError,ValueError):
            unchanged=False
        if not unchanged:
            raise ValueError("The local harness changed. Reconnect and review its permissions before running it.")

    def manifest(self):
        return [{"id":key,"name":str(value.get("name",key))[:80],"description":str(value.get("description","Local agent adapter"))[:250],"revision":self.revisions[key]}
                for key,value in self.harnesses.items()]

    def capabilities(self):
        return (["command.run"] if self.commands else []) + (["harness.run"] if self.harnesses else [])

    def prepare(self, capability, args):
        timeout = args.get("timeout",120)
        if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not 1 <= timeout <= 300:
            raise ValueError("Timeout must be between 1 and 300 seconds.")
        cwd = self.folder.path(args.get("cwd","."))
        if not cwd.is_dir():
            raise ValueError("Working directory must be a shared folder.")
        if capability == "command.run":
            if not self.commands:
                raise ValueError("Commands are disabled on this computer.")
            argv = args.get("argv")
            if not isinstance(argv,list) or not 1 <= len(argv) <= 64 or any(not isinstance(x,str) or "\0" in x or len(x)>16000 for x in argv) or not argv[0]:
                raise ValueError("Provide a nonempty argv array, at most 64 arguments.")
            if Path(argv[0]).suffix.lower() in {".bat",".cmd"}:
                raise ValueError("Use an executable directly. Batch files require an explicit command interpreter in argv.")
            summary = "Command:\n" + json.dumps(argv,ensure_ascii=False,indent=2)
            return argv,cwd,timeout,None,summary
        if capability != "harness.run":
            raise ValueError("Unsupported local process capability.")
        if any(key in args for key in ("executable","argv","command","adapter_args")):
            raise ValueError("Executable settings can only be changed on this computer.")
        adapter = self.harnesses.get(args.get("harness_id"))
        if adapter is None:
            raise ValueError("Select a locally registered harness.")
        self.check_revision(args["harness_id"])
        goal = args.get("goal")
        context = args.get("context",{})
        if not isinstance(goal,str) or not goal.strip() or len(goal)>24000 or not isinstance(context,dict):
            raise ValueError("Provide a goal and an optional context object.")
        executable = Path(adapter["executable"]).expanduser().resolve(strict=True)
        if not executable.is_file() or executable.suffix.lower() in {".bat",".cmd"}:
            raise ValueError("Register a local executable, not a batch file.")
        local_args = adapter.get("arguments",[])
        if not isinstance(local_args,list) or any(not isinstance(item,str) or "\0" in item for item in local_args):
            raise ValueError("Local adapter arguments must be a JSON string array.")
        if adapter.get("kind") == "hermes":
            argv = [str(executable),"chat","--quiet","--ignore-rules","--source","tool","--max-turns","8","--toolsets","terminal","--query",goal]
            payload = None
        else:
            argv = [str(executable)] + local_args
            payload = json.dumps({"version":1,"goal":goal,"context":context}).encode()
            if len(payload)>64000:
                raise ValueError("Harness input must be 64 KB or smaller.")
        summary = "Harness: " + adapter.get("name",args["harness_id"]) + "\nExecutable: " + str(executable) + "\nLocal arguments: " + json.dumps(local_args) + "\n\nGoal:\n" + goal + "\n\nContext:\n" + json.dumps(context,ensure_ascii=False,indent=2)
        return argv,cwd,timeout,payload,summary

    def execute(self, capability, args, cancel, progress=lambda _:None, dashboard_approved=False):
        argv,cwd,timeout,payload,summary = self.prepare(capability,args)
        details = summary + "\n\nWorking directory: " + str(cwd) + f"\nTime limit: {timeout:g} seconds\n\nThis process runs as your operating-system account. It can access files and network resources outside the shared folder."
        if not (self.remote_approval and dashboard_approved):
            progress("Waiting for confirmation on the computer (expires after 60 seconds).")
            if not self.confirm(details,cancel) or cancel.is_set():
                raise Cancelled("Local confirmation declined, expired, or cancelled. The action was not started.")
        elif cancel.is_set():
            raise Cancelled("Cancelled before execution.")
        if capability == "harness.run":
            self.check_revision(args["harness_id"])
        progress("Starting approved local process.")
        result = run_process(argv,cwd,timeout,cancel,payload,progress)
        if capability == "harness.run":
            try:
                result["output"] = json.loads(result["stdout"])
                result["stdout"] = ""
            except ValueError:
                pass
        return result


def hermes_registration():
    executable = shutil.which("hermes")
    return None if not executable else {"name":"Hermes","description":"Local Hermes CLI, terminal tools, at most eight turns and five minutes.","executable":executable,"arguments":[],"kind":"hermes"}
