import json
import logging

from fastapi import Request
from starlette.concurrency import iterate_in_threadpool

logger = logging.getLogger("Andromeda.Core")

async def log_requests_middleware(request: Request, call_next):
    # Skip logging for log polling and SSE endpoints to avoid spam/crashes
    if request.url.path.startswith("/logs") or request.url.path == "/client/events":
        return await call_next(request)

    # Capture Request Body (only for non-GET)
    req_body_str = ""
    if request.method != "GET":
        try:
            req_body_bytes = await request.body()
            # Restore body for downstream
            async def receive():
                return {"type": "http.request", "body": req_body_bytes}
            request._receive = receive
            
            req_is_json = "application/json" in request.headers.get("content-type", "")
            req_body_str = req_body_bytes.decode("utf-8", errors="replace") if req_body_bytes else ""
            if req_is_json and req_body_str: 
                try:
                    req_body_str = json.dumps(json.loads(req_body_str))
                except: 
                    pass
        except:
            pass

    response = await call_next(request)
    
    # DO NOT capture body for StreamingResponse or SSE
    # Starlette's BaseHTTPMiddleware + StreamingResponse = Disaster if you touch the body_iterator
    is_streaming = "text/event-stream" in response.headers.get("content-type", "")
    
    resp_body_str = "<streaming>"
    if not is_streaming:
        try:
            # Capture Response Body
            resp_body_bytes = b""
            async for chunk in response.body_iterator:
                resp_body_bytes += chunk
            
            # Re-create the iterator for the actual response
            response.body_iterator = iterate_in_threadpool(iter([resp_body_bytes]))
            resp_body_str = resp_body_bytes.decode("utf-8", errors="replace")
        except:
            resp_body_str = "<error-capturing-body>"
    
    # Format Log as JSON
    log_data = {
        "level": "info" if response.status_code < 400 else "error",
        "service": "api",
        "method": request.method,
        "path": request.url.path,
        "status": response.status_code,
        "environment": "production", # or pull from settings
        "message": f"API request: {request.method} {request.url.path} -> {response.status_code}",
        "request_body": req_body_str,
        "response_body": resp_body_str,
        "client_ip": request.client.host if request.client else None,
    }
    
    # Send to Log Server via TCP (Fire and Forget)
    try:
        import socket
        msg = json.dumps(log_data) + "\n"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.1)
        sock.connect(('127.0.0.1', 9090))
        sock.sendall(msg.encode('utf-8'))
        sock.close()
    except:
        pass
        
    return response
