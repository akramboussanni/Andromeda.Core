import socketserver
import threading
import time
from typing import List
from collections import deque

# Global log storage (circular buffer behavior)
LOG_BUFFER: deque = deque(maxlen=5000)

class LogHandler(socketserver.BaseRequestHandler):
    def handle(self):
        # self.request is the TCP socket connected to the client
        try:
            while True:
                data = self.request.recv(1024)
                if not data:
                    break
                
                message = data.decode('utf-8', errors='ignore')
                # Split by newline in case multiple logs come in one packet
                lines = message.split('\n')
                for line in lines:
                    if line.strip():
                        # Add timestamp
                        timestamp = time.strftime("%H:%M:%S")
                        entry = f"[{timestamp}] {line.strip()}"
                        print(f"GAME LOG: {entry}") # Print to server console too
                        LOG_BUFFER.append(entry)
                        
                        # Persist to file
                        with open("game_logs.txt", "a", encoding="utf-8") as f:
                            f.write(entry + "\n")
                        
        except Exception as e:
            print(f"Log connection error: {e}")

class LogServerThread(threading.Thread):
    def __init__(self, host='0.0.0.0', port=9090):
        super().__init__()
        self.server = socketserver.TCPServer((host, port), LogHandler)
        self.daemon = True

    def run(self):
        print(f"Log Server listening on port 9090...")
        self.server.serve_forever()

def start_log_server():
    server_thread = LogServerThread()
    server_thread.start()
