import threading
import socket
import json
import os
import time
import subprocess

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
        self.socket = None

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
        self.socket = Win32Pipe(self.ipc_socket, client=True)
        try:
            while True:
                current_data = self.socket.read(2048)
                if current_data == b'':
                    break

                data += current_data
                if data[-1] != 10:
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
        self.socket = None

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
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.connect(self.ipc_socket)
        data = b''
        while True:
            current_data = self.socket.recv(1024)
            if current_data == b'':
                break

            data += current_data
            if data[-1] != 10:
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

