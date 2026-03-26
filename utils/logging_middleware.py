import json
import logging

from fastapi import Request
from starlette.concurrency import iterate_in_threadpool

logger = logging.getLogger("Andromeda.Core")

async def log_requests_middleware(request: Request, call_next):
    # Skip logging for log polling endpoints to avoid spam
    if request.url.path.startswith("/logs"):
        return await call_next(request)

    # Capture Request Body
    req_body_bytes = await request.body()
    # Restore body for downstream
    async def receive():
        return {"type": "http.request", "body": req_body_bytes}
    request._receive = receive
    
    req_is_json = "application/json" in request.headers.get("content-type", "")
    req_body_str = req_body_bytes.decode("utf-8", errors="replace") if req_body_bytes else ""
    if req_is_json: 
        # Clean up whitespace for nicer logs if strictly JSON
        try:
            req_body_str = json.dumps(json.loads(req_body_str))
        except: 
            pass

    response = await call_next(request)
    
    # Capture Response Body
    # We need to re-construct the response chunks to read them without consuming them forever
    resp_body_bytes = b""
    async for chunk in response.body_iterator:
        resp_body_bytes += chunk
    
    # Re-create the iterator for the actual response
    response.body_iterator = iterate_in_threadpool(iter([resp_body_bytes]))
    
    resp_body_str = resp_body_bytes.decode("utf-8", errors="replace")
    
    # Format Log
    log_entry = f"UNKNOWN | {request.method} {request.url.path}\nREQ: {req_body_str}\nRESP ({response.status_code}): {resp_body_str}"
    
    # Send to Log Server (Fire and Forget-ish)
    try:
        # sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # sock.settimeout(0.1)
        # sock.connect(('127.0.0.1', 9090))
        # sock.sendall(log_entry.encode('utf-8'))
        # sock.close()
        pass
    except Exception as e:
        # Don't break the server if logging fails
        print(f"Failed to send log to log server: {e}")
        
    return response
