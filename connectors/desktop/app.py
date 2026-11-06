"""HiveMind desktop setup and node window."""
from __future__ import annotations

import csv
import io
import json
import os
import platform
import plistlib
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from urllib.error import HTTPError
from tkinter import filedialog, messagebox, ttk

from node import CAPABILITIES, Client, Folder, Journal, Node, server_url
from harness import Runner, hermes_registration

CONFIRM_SECONDS = 60


def data_dir():
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "HiveMind"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "HiveMind"
    return Path.home() / ".local" / "share" / "hivemind"


def protect(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True, creationflags=0x08000000)
        sid = next(csv.reader(io.StringIO(result.stdout.strip())))[1]
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"], capture_output=True, check=True, creationflags=0x08000000)
    else:
        path.chmod(0o700)


def save_config(path: Path, config: dict):
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as target:
        json.dump(config, target)
    if os.name != "nt":
        temp.chmod(0o600)
    temp.replace(path)


def install_windows():
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    destination = data_dir() / "HiveMind.exe"
    if Path(sys.executable).resolve() != destination.resolve():
        shutil.copy2(sys.executable, destination)
    desktop = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "HiveMind.lnk"
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    script = "$w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut(" + quote(desktop) + "); $s.TargetPath=" + quote(destination) + "; $s.WorkingDirectory=" + quote(destination.parent) + "; $s.Save()"
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], check=True, capture_output=True, creationflags=0x08000000)


def startup_setting(enabled):
    if os.name == "nt":
        import winreg
        if enabled and not getattr(sys,"frozen",False):
            raise ValueError("Start at sign-in requires the installed Windows app.")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            if enabled:
                winreg.SetValueEx(key,"HiveMind",0,winreg.REG_SZ,'"'+str(data_dir()/"HiveMind.exe")+'"')
            else:
                try:
                    winreg.DeleteValue(key,"HiveMind")
                except FileNotFoundError:
                    pass
    elif sys.platform == "darwin":
        plist = Path.home()/"Library"/"LaunchAgents"/"org.hivemind.connector.plist"
        if enabled:
            app_dir=data_dir()/"app"
            app_dir.mkdir(exist_ok=True)
            for filename in ("app.py","node.py","harness.py"):
                source=Path(__file__).parent/filename
                destination=app_dir/filename
                if source.resolve()!=destination.resolve():
                    shutil.copy2(source,destination)
            plist.parent.mkdir(parents=True,exist_ok=True)
            with plist.open("wb") as target:
                plistlib.dump({"Label":"org.hivemind.connector","ProgramArguments":[sys.executable,str(app_dir/"app.py")],"RunAtLoad":True,"KeepAlive":False},target)
        else:
            plist.unlink(missing_ok=True)


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("HiveMind")
        self.root.geometry("720x760")
        self.root.minsize(660, 700)
        self.folder = data_dir()
        protect(self.folder)
        self.config_path = self.folder / "connection.json"
        self.config = {}
        if self.config_path.exists():
            self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.messages = queue.Queue()
        self.node = None
        self.thread = None
        self.starting = False
        self.closing = False
        self.stop_requested = threading.Event()
        self.revocation_requested = threading.Event()
        self.config_lock = threading.RLock()
        self.pairing_uncertain = self.config.get("pairing_uncertain",False)
        self.forgetting = False
        style = ttk.Style()
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=7)
        outer = ttk.Frame(self.root,padding=22)
        outer.pack(fill="both",expand=True)
        ttk.Label(outer,text="HiveMind",font=("Segoe UI",22,"bold")).pack(anchor="w")
        ttk.Label(outer,text="Connect this computer and choose what it can do.").pack(anchor="w",pady=(6,14))
        tabs=ttk.Notebook(outer)
        tabs.pack(fill="both",expand=True)
        panel=ttk.Frame(tabs,padding=18)
        access=ttk.Frame(tabs,padding=18)
        adapters=ttk.Frame(tabs,padding=18)
        startup=ttk.Frame(tabs,padding=18)
        tabs.add(panel,text="Connection")
        tabs.add(access,text="Access")
        tabs.add(adapters,text="Agents")
        tabs.add(startup,text="Startup")
        self.server = tk.StringVar(value=self.config.get("server", ""))
        self.code = tk.StringVar()
        self.name = tk.StringVar(value=self.config.get("name", socket.gethostname()))
        self.shared = tk.StringVar(value=self.config.get("folder", ""))
        self.commands=tk.BooleanVar(value=self.config.get("commands",False))
        self.remote_approval=tk.BooleanVar(value=self.config.get("remote_approval",False))
        self.remote_approval_server=self.config.get("server","") if self.config.get("remote_approval") else ""
        self.custom=tk.BooleanVar(value=self.config.get("custom_enabled",False))
        self.hermes=tk.BooleanVar(value=self.config.get("hermes_enabled",False))
        self.reconnect=tk.BooleanVar(value=self.config.get("reconnect",False))
        self.startup=tk.BooleanVar(value=self.config.get("startup",False))
        self.adapter_name=tk.StringVar(value=self.config.get("adapter_name","My agent"))
        self.adapter_executable=tk.StringVar(value=self.config.get("adapter_executable",""))
        self.adapter_arguments=tk.StringVar(value=self.config.get("adapter_arguments","[]"))
        for label, variable in [("HiveMind server address", self.server), ("Pairing code from Devices in your dashboard", self.code), ("Computer name", self.name)]:
            ttk.Label(panel, text=label).pack(anchor="w")
            ttk.Entry(panel, textvariable=variable).pack(fill="x", pady=(3, 11))
        ttk.Label(panel, text="Shared folder").pack(anchor="w")
        row = ttk.Frame(panel)
        row.pack(fill="x", pady=(3, 16))
        ttk.Entry(row, textvariable=self.shared).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Choose…", command=self.choose).pack(side="left", padx=(7, 0))
        ttk.Label(panel,text="File tools stay inside this folder. Choose an approval level in Devices\non your dashboard. Commands and agents have separate permissions in Access.",foreground="#555555").pack(anchor="w",pady=10)
        ttk.Checkbutton(access,text="Allow commands",variable=self.commands).pack(anchor="w")
        ttk.Label(access,text="Commands run as your account and may access files outside the shared\nfolder. Each command needs approval in HiveMind.",foreground="#555555").pack(anchor="w",pady=(3,8))
        ttk.Checkbutton(access,text="Accept command and agent approvals from HiveMind",variable=self.remote_approval,command=self.review_remote_approval).pack(anchor="w")
        ttk.Label(access,text="When enabled, your dashboard approval is enough. Otherwise this computer\nasks too. Enable this only for a server you trust.",foreground="#555555").pack(anchor="w",pady=(3,16))
        ttk.Label(access,text="Register local agent programs in Agents. They always need dashboard\napproval and follow the confirmation setting above.",foreground="#555555").pack(anchor="w",pady=10)
        ttk.Checkbutton(adapters,text="Connect a custom agent harness",variable=self.custom).pack(anchor="w")
        for label,variable in [("Agent name",self.adapter_name),("Executable on this computer",self.adapter_executable),("Fixed arguments (JSON list, for example [\"agent.py\"])",self.adapter_arguments)]:
            ttk.Label(adapters,text=label).pack(anchor="w",pady=(8,2))
            frame=ttk.Frame(adapters)
            frame.pack(fill="x")
            ttk.Entry(frame,textvariable=variable).pack(side="left",fill="x",expand=True)
            if variable is self.adapter_executable:
                ttk.Button(frame,text="Choose…",command=self.choose_adapter).pack(side="left",padx=(6,0))
        ttk.Label(adapters,text="The adapter receives the approved goal as JSON on standard input.\nIts executable and fixed arguments can only be changed here.",foreground="#555555").pack(anchor="w",pady=10)
        hermes_toggle=ttk.Checkbutton(adapters,text="Connect installed Hermes (up to 8 turns, terminal tools)",variable=self.hermes)
        hermes_toggle.pack(anchor="w",pady=8)
        if not hermes_registration():
            self.hermes.set(False)
            hermes_toggle.configure(state="disabled")
        ttk.Checkbutton(startup,text="Reconnect automatically when this app opens",variable=self.reconnect).pack(anchor="w")
        startup_toggle=ttk.Checkbutton(startup,text="Open HiveMind when I sign in",variable=self.startup)
        startup_toggle.pack(anchor="w",pady=5)
        if (os.name=="nt" and not getattr(sys,"frozen",False)) or (os.name!="nt" and sys.platform!="darwin"):
            startup_toggle.configure(state="disabled")
        ttk.Label(access,text="Changes take effect the next time you connect.",foreground="#555555").pack(anchor="w",pady=10)
        ttk.Label(adapters,text="Changes take effect the next time you connect.",foreground="#555555").pack(anchor="w",pady=10)
        ttk.Label(startup,text="Changes take effect the next time you connect.",foreground="#555555").pack(anchor="w",pady=10)
        self.status = tk.StringVar(value="Disconnect is pending. Retry when online." if self.config.get("revocation_pending") else "Ready. Pairing codes expire after 10 minutes.")
        actions=ttk.Frame(outer)
        actions.pack(fill="x",pady=(16,0))
        self.connect_button = ttk.Button(actions, text="Connect", command=self.connect)
        self.connect_button.pack(side="left")
        self.disconnect_button = ttk.Button(actions, text="Disconnect", command=self.disconnect, state="disabled")
        self.disconnect_button.pack(side="left",padx=8)
        self.forget_button=ttk.Button(actions,text="Forget device",command=self.forget)
        self.forget_button.pack(side="right")
        ttk.Label(outer, textvariable=self.status, wraplength=670).pack(anchor="w", pady=12)
        ttk.Label(outer,text="Close pauses this app and keeps pairing. Disconnect removes its connection.",foreground="#555555").pack(anchor="w")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(150, self.drain)
        self.refresh_buttons()
        if self.reconnect.get() and self.config.get("token") and not self.config.get("revocation_pending"):
            self.root.after(400,self.connect)

    def persist(self, config):
        with self.config_lock:
            self.config=dict(config)
            save_config(self.config_path,self.config)

    def refresh_buttons(self):
        alive=bool(self.thread and self.thread.is_alive())
        pending=self.config.get("revocation_pending",False)
        self.connect_button.configure(state="disabled" if self.starting or alive or self.forgetting or pending else "normal")
        self.disconnect_button.configure(text="Retry disconnect" if pending else "Disconnect",
            state="normal" if not self.forgetting and (alive or self.starting or self.config.get("token") or pending) else "disabled")
        self.forget_button.configure(state="disabled" if self.forgetting else "normal")
        if alive and not self.starting and not self.forgetting:
            self.root.after(100,self.refresh_buttons)

    def choose(self):
        selected = filedialog.askdirectory(title="Choose the only folder HiveMind may use")
        if selected:
            self.shared.set(selected)

    def choose_adapter(self):
        selected=filedialog.askopenfilename(title="Choose the local agent executable")
        if selected:
            self.adapter_executable.set(selected)

    def review_remote_approval(self):
        self.remote_approval_server=self.server.get().strip().rstrip("/") if self.remote_approval.get() else ""

    def confirm(self,details,cancel):
        ready=threading.Event()
        result={"allow":False}
        deadline=time.monotonic()+CONFIRM_SECONDS
        self.messages.put(("confirm",details,cancel,ready,result,deadline))
        while not ready.wait(0.2):
            if cancel.is_set() or self.stop_requested.is_set() or time.monotonic()>deadline:
                if time.monotonic()>deadline:
                    self.messages.put("Local confirmation expired. The action was not started.")
                cancel.set()
                return False
        return result["allow"]

    def confirmation_window(self,details,cancel,ready,result,deadline):
        if cancel.is_set() or self.stop_requested.is_set():
            ready.set()
            return
        window=tk.Toplevel(self.root)
        window.title("Approve local action")
        window.geometry("720x580")
        window.transient(self.root)
        ttk.Label(window,text="Allow this action on this computer?",font=("Segoe UI",15,"bold"),padding=15).pack(anchor="w")
        countdown=tk.StringVar()
        ttk.Label(window,textvariable=countdown,padding=(15,0,15,10)).pack(anchor="w")
        area=tk.Text(window,wrap="word",font=("Consolas",10),padx=14,pady=12)
        area.insert("1.0",details)
        area.configure(state="disabled")
        area.pack(fill="both",expand=True,padx=12)
        def finish(allow):
            result["allow"]=allow
            ready.set()
            window.destroy()
        buttons=ttk.Frame(window,padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons,text="Decline",command=lambda:finish(False)).pack(side="right")
        ttk.Button(buttons,text="Allow once",command=lambda:finish(True)).pack(side="right",padx=8)
        window.protocol("WM_DELETE_WINDOW",lambda:finish(False))
        def check():
            if not window.winfo_exists():
                return
            remaining=max(0,int(deadline-time.monotonic()))
            countdown.set(f"Waiting for your decision. Expires in {remaining} seconds.")
            if cancel.is_set() or self.stop_requested.is_set() or remaining==0:
                cancel.set()
                finish(False)
            else:
                window.after(200,check)
        check()

    def connect(self):
        if self.starting or self.forgetting or self.config.get("revocation_pending") or (self.thread and self.thread.is_alive()):
            return
        try:
            address = server_url(self.server.get())
            if self.remote_approval.get() and self.remote_approval_server != address:
                self.remote_approval.set(False)
                raise ValueError("The server address changed. Review the confirmation setting in Access before connecting.")
            shared_folder = Folder(self.shared.get())
            name = self.name.get().strip()[:80]
            if not name:
                raise ValueError("Give this computer a name.")
            code = self.code.get().strip()
            if self.config.get("token") and (code or self.config.get("server")!=address):
                raise ValueError("Disconnect the saved device before using a new pairing code or server.")
            cached = self.config if self.config.get("server") == address else {}
            if not code and not cached.get("token"):
                raise ValueError("Create a pairing code in your dashboard's Devices section.")
            harnesses={}
            if self.custom.get():
                executable=Path(self.adapter_executable.get()).expanduser().resolve(strict=True)
                arguments=json.loads(self.adapter_arguments.get())
                if not executable.is_file() or not isinstance(arguments,list) or any(not isinstance(item,str) for item in arguments):
                    raise ValueError("Choose an executable and a JSON list of fixed arguments.")
                harnesses["custom"]={"name":self.adapter_name.get().strip()[:80] or "My agent","description":"Locally registered agent adapter","executable":str(executable),"arguments":arguments}
            if self.hermes.get():
                registered=hermes_registration()
                if not registered:
                    raise ValueError("Hermes is not installed on this computer.")
                harnesses["hermes"]=registered
            runner=Runner(shared_folder,self.commands.get(),harnesses,self.confirm,remote_approval=self.remote_approval.get())
            preferences={"commands":self.commands.get(),"custom_enabled":self.custom.get(),"hermes_enabled":self.hermes.get(),
                         "remote_approval":self.remote_approval.get(),
                         "adapter_name":self.adapter_name.get(),"adapter_executable":self.adapter_executable.get(),"adapter_arguments":self.adapter_arguments.get(),
                         "reconnect":self.reconnect.get(),"startup":self.startup.get()}
            prior_startup=self.config.get("startup",False)
        except Exception as error:
            messagebox.showerror("Check connection settings", str(error))
            return
        self.starting = True
        self.stop_requested.clear()
        self.revocation_requested.clear()
        self.pairing_uncertain=False
        self.connect_button.configure(state="disabled")
        self.disconnect_button.configure(state="normal")
        self.status.set("Connecting…")
        def worker():
            try:
                client = Client(address, cached.get("token", "") if not code else "")
                config = {key:cached[key] for key in ("token","node_id") if key in cached}
                if self.revocation_requested.is_set():
                    return
                if code:
                    self.pairing_uncertain=True
                    paired = client.call("POST", "/api/nodes/pair", {"code": code, "name": name, "kind": "mac" if sys.platform == "darwin" else "pc", "capabilities": CAPABILITIES+runner.capabilities(),"harnesses":runner.manifest(),"scope":shared_folder.scope(),"local_confirmation":not runner.remote_approval})
                    self.pairing_uncertain=False
                    config.update(paired)
                    client.token = paired["token"]
                config.update(server=address, name=name, folder=str(shared_folder.root))
                config.update(preferences)
                with self.config_lock:
                    if self.revocation_requested.is_set():
                        config.update(reconnect=False,revocation_pending=True)
                    self.persist(config)
                if self.stop_requested.is_set():
                    return
                client.call("POST", "/api/node/heartbeat", {"capabilities":CAPABILITIES+runner.capabilities(),"harnesses":runner.manifest(),"scope":shared_folder.scope(),"local_confirmation":not runner.remote_approval})
                if not self.revocation_requested.is_set() and preferences["startup"] != prior_startup:
                    startup_setting(preferences["startup"])
                if self.stop_requested.is_set():
                    return
                journal = Journal(self.folder / ("tasks-" + config["node_id"] + ".sqlite3"))
                self.node = Node(client, shared_folder, journal, self.messages.put,runner)
                self.messages.put("Connected. Waiting for an approved task.")
                if self.closing or self.stop_requested.is_set():
                    self.node.disconnect()
                self.node.run()
            except Exception as error:
                if self.pairing_uncertain:
                    if isinstance(error,HTTPError) and error.code in (400,401,403,422,429):
                        self.pairing_uncertain=False
                    else:
                        with self.config_lock:
                            saved=dict(self.config)
                            saved.pop("token",None)
                            saved.pop("node_id",None)
                            saved.update(server=address,pairing_uncertain=True,revocation_pending=True,reconnect=False)
                            self.persist(saved)
                if not self.revocation_requested.is_set():
                    self.messages.put("Could not connect. Check the address and pairing code, or create a new code.")
            finally:
                self.messages.put("__stopped__")
        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def pause_local(self):
        self.stop_requested.set()
        if self.node:
            self.node.disconnect()

    def disconnect(self):
        self.begin_revoke(clear_preferences=False)

    def begin_revoke(self,clear_preferences=False):
        if self.forgetting:
            return
        self.forgetting=True
        self.revocation_requested.set()
        self.pause_local()
        self.reconnect.set(False)
        with self.config_lock:
            config=dict(self.config)
            config.update(reconnect=False,revocation_pending=True)
            self.persist(config)
        self.status.set("Stopping local work and removing this device from HiveMind…")
        self.refresh_buttons()
        def revoke():
            if self.thread:
                self.thread.join(timeout=45)
                if self.thread.is_alive():
                    self.messages.put(("revoke_failed","Local work is stopping. Server disconnect is still pending; retry shortly."))
                    return
            with self.config_lock:
                saved=dict(self.config)
            try:
                if saved.get("token"):
                    try:
                        Client(saved["server"],saved["token"]).call("POST","/api/node/disconnect",{},timeout=10)
                    except HTTPError as error:
                        if error.code!=401:
                            raise
                elif self.pairing_uncertain:
                    self.messages.put(("revoke_failed","Stopped locally. Pairing was interrupted; remove any new device in the dashboard, then retry."))
                    return
                self.messages.put(("revoked",clear_preferences))
            except Exception:
                self.messages.put(("revoke_failed","Stopped locally. Server disconnect is still pending. Retry when online or remove this device in the dashboard."))
        threading.Thread(target=revoke,daemon=True).start()

    def drain(self):
        while not self.messages.empty():
            message = self.messages.get_nowait()
            if isinstance(message,tuple):
                if message[0]=="confirm":
                    self.confirmation_window(*message[1:])
                elif message[0]=="revoked":
                    clear_preferences=message[1]
                    if clear_preferences and self.config.get("startup"):
                        startup_setting(False)
                    config={} if clear_preferences else {key:value for key,value in self.config.items() if key not in ("token","node_id","revocation_pending","pairing_uncertain")}
                    config["reconnect"]=False
                    self.persist(config)
                    self.pairing_uncertain=False
                    if clear_preferences:
                        self.server.set("")
                    self.code.set("")
                    self.forgetting=False
                    self.refresh_buttons()
                    self.status.set("Disconnected from HiveMind. Pair again to reconnect.")
                    if self.closing:
                        self.root.destroy()
                        return
                elif message[0]=="revoke_failed":
                    self.forgetting=False
                    self.refresh_buttons()
                    self.status.set(message[1])
                    if self.closing:
                        self.root.destroy()
                        return
            elif message == "__stopped__":
                self.starting = False
                self.node = None
                self.refresh_buttons()
                if self.closing and not self.forgetting:
                    self.root.destroy()
                    return
            elif not self.forgetting:
                self.status.set(message)
        self.root.after(150, self.drain)

    def forget(self):
        if self.forgetting:
            return
        if self.pairing_uncertain and not self.config.get("token"):
            if messagebox.askyesno("Forget interrupted pairing","The pairing response was lost. Remove any newly added device in your dashboard first. Clear this computer's interrupted setup now?"):
                self.pause_local()
                if self.config.get("startup"):
                    startup_setting(False)
                self.persist({})
                self.pairing_uncertain=False
                self.reconnect.set(False)
                self.server.set("")
                self.code.set("")
                self.refresh_buttons()
                self.status.set("Local setup cleared. Any unknown server device must be removed in the dashboard.")
            return
        if not messagebox.askyesno("Forget device","Revoke this device on its saved server and remove its local connection?"):
            return
        self.begin_revoke(clear_preferences=True)

    def close(self):
        self.closing = True
        self.pause_local()
        if not self.forgetting and (not self.thread or not self.thread.is_alive()):
            self.root.destroy()


if __name__ == "__main__":
    try:
        protect(data_dir())
        install_windows()
        App().root.mainloop()
    except Exception as error:
        messagebox.showerror("HiveMind", "Setup could not finish: " + error.__class__.__name__)
