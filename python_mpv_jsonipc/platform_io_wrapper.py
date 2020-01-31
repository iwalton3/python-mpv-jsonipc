import threading
import socket
import json

class UnixSocket(threading.Thread):
    def __init__(self, ipc_socket, callback=None):
        self.ipc_socket = ipc_socket
        self.callback = callback
        self.socket = None

        if self.callback is None:
            self.callback = lambda event, data: None

        threading.Thread.__init__(self)

    def stop(self):
        if self.socket is not None:
            self.socket.close()
        self.join()

    def send(self, command, *args):
        command_list = [command]
        command_list.extend(args)
        self.socket.send(json.dumps({"command":command_list }).encode('utf-8') + b'\n')

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
            data = b''
            print(json_data)

