import threading
import socket
import json
import os
import time
import subprocess
import random

if os.name == "nt":
    from .win32_named_pipe import Win32Pipe

TIMEOUT = 120

class MPVError(Exception):
    def __init__(self, *args, **kwargs):
        super(MPVError, self).__init__(*args, **kwargs)

class WindowsSocket(threading.Thread):
    def __init__(self, ipc_socket, callback=None):
        self.ipc_socket = ipc_socket
        self.callback = callback
        self.socket = Win32Pipe(self.ipc_socket, client=True)

        if self.callback is None:
            self.callback = lambda data: None

        threading.Thread.__init__(self)

    def stop(self):
        if self.socket is not None:
            self.socket.close()
        self.join()

    def send(self, data):
        self.socket.write(json.dumps(data).encode('utf-8') + b'\n')

    def run(self):
        data = b''
        try:
            while True:
                current_data = self.socket.read(2048)
                if current_data == b'':
                    break

                data += current_data
                if data[-1] != 10:
                    continue

                for item in data.split(b'\n'):
                    if item == '':
                        continue
                    json_data = json.loads(data)
                    self.callback(json_data)
                data = b''
        except BrokenPipeError:
            pass

class UnixSocket(threading.Thread):
    def __init__(self, ipc_socket, callback=None):
        self.ipc_socket = ipc_socket
        self.callback = callback
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.connect(self.ipc_socket)

        if self.callback is None:
            self.callback = lambda data: None

        threading.Thread.__init__(self)

    def stop(self):
        if self.socket is not None:
            self.socket.shutdown(socket.SHUT_WR)
            self.socket.close()
        self.join()

    def send(self, data):
        self.socket.send(json.dumps(data).encode('utf-8') + b'\n')

    def run(self):
        data = b''
        while True:
            current_data = self.socket.recv(1024)
            if current_data == b'':
                break

            data += current_data
            if data[-1] != 10:
                continue

            for item in data.split(b'\n'):
                if item == '':
                    continue
                json_data = json.loads(data)
                self.callback(json_data)
            data = b''

class MPVProcess:
    def __init__(self, ipc_socket, mpv_location=None, **kwargs):
        if mpv_location is None:
            if os.name == 'nt':
                mpv_location = "mpv.exe"
            else:
                mpv_location = "mpv"
        
        if os.name == 'nt':
            ipc_socket = "\\\\.\\pipe\\" + ipc_socket

        if os.path.exists(ipc_socket):
            os.remove(ipc_socket)

        args = [mpv_location]
        args.extend("--{0}={1}".format(*v) for v in kwargs.items())        
        self.process = subprocess.Popen(args)
        for _ in range(20):
            time.sleep(0.1)
            self.process.poll()
            if os.path.exists(ipc_socket) or self.process.returncode is not None:
                break
        
        if not os.path.exists(ipc_socket) or self.process.returncode is not None:
            raise MPVError("MPV not started.")

    def stop(self):
        self.process.terminate()

class MPVInter:
    def __init__(self, ipc_socket, callback=None):
        Socket = UnixSocket
        if os.name == 'nt':
            Socket = WindowsSocket

        self.callback = callback
        if self.callback is None:
            self.callback = lambda event, data: None
        
        self.socket = Socket(ipc_socket, self.event_callback)
        self.socket.start()
        self.command_id = 0
        self.rid_lock = threading.Lock()
        self.socket_lock = threading.Lock()
        self.cid_result = {}
        self.cid_wait = {}
    
    def stop(self):
        self.socket.stop()

    def event_callback(self, data):
        if "request_id" in data:
            self.cid_result[data["request_id"]] = data
            self.cid_wait[data["request_id"]].set()
        elif "event" in data:
            self.callback(data["event"], data)
    
    def command(self, command, *args):
        self.rid_lock.acquire()
        command_id = self.command_id
        self.command_id += 1
        self.rid_lock.release()

        event = threading.Event()
        self.cid_wait[command_id] = event

        command_list = [command]
        command_list.extend(args)
        try:
            self.socket_lock.acquire()
            self.socket.send({"command":command_list, "request_id": command_id})
        finally:
            self.socket_lock.release()

        has_event = event.wait(timeout=TIMEOUT)
        if has_event:
            data = self.cid_result[command_id]
            del self.cid_result[command_id]
            del self.cid_wait[command_id]
            if data["error"] != "success":
                raise MPVError(data["error"])
            else:
                return data["data"]
        else:
            raise TimeoutError("No response from MPV.")

class MPV:
    def __init__(self, start_mpv=True, ipc_socket=None, mpv_location=None, **kwargs):
        self.properties = {}
        self.mpv_process = None
        if ipc_socket is None:
            rand_file = "mpv{0}".format(random.randint(0, 2**48))
            if os.name == "nt":
                ipc_socket = rand_file
            else:
                ipc_socket = "/tmp/{0}".format(rand_file)

        if start_mpv:
            self.mpv_process = MPVProcess(ipc_socket, mpv_location, **kwargs)

        self.mpv_inter = MPVInter(ipc_socket, self._callback)
        self.properties = set(x.replace("-", "_") for x in self.command("get_property", "property-list"))
        for command in self.command("get_property", "command-list"):
            def wrapper(*args):
                self.command(command, *args)
            object.__setattr__(self, command["name"].replace("-", "_"), wrapper)

        self._dir = list(self.properties)
        self._dir.extend(object.__dir__(self))

    def _callback(self, event, data):
        pass

    def command(self, command, *args):
        return self.mpv_inter.command(command, *args)

    def __getattr__(self, name):
        if name in self.properties:
            return self.command("get_property", name.replace("_", "-"))
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        if name not in {"properties", "command"} and name in self.properties:
            return self.command("set_property", name.replace("_", "-"), value)
        return object.__setattr__(self, name, value)

    def __dir__(self):
        return self._dir

#osd_sym_cc - Invalid, need to fix JSON
#property unavailable -> None
#property not found