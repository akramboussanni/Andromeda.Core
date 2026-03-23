import upnpy
import socket

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

def open_port(port, protocol='TCP', description='Parasite Python Server'):
    """
    Hyper-robust UPnP port opening.
    Iterates through all discovered devices and skips those with malformed XML.
    """
    try:
        upnp = upnpy.UPnP()
        print(f"[UPnP] Searching for router to open {port}...")
        
        # We manually use the SSDP search to skip the 'discover()' wall of errors
        ssdp = upnp.ssdp
        responses = ssdp.m_search(st='urn:schemas-upnp-org:device:InternetGatewayDevice:1')
        
        if not responses:
            print("[UPnP] No Internet Gateway Devices responded to search.")
            return False

        for response in responses:
            try:
                # Manual device creation to catch XML parsing errors per-device
                location = response.get_header('location')
                device = upnpy.upnp.Device(location)
                
                # Look for the port mapping service
                service = None
                for s in device.get_services():
                    actions = [a.name for a in s.get_actions()]
                    if 'AddPortMapping' in actions:
                        service = s
                        break
                
                if service:
                    local_ip = get_local_ip()
                    service.AddPortMapping(
                        NewRemoteHost='',
                        NewExternalPort=port,
                        NewProtocol=protocol,
                        NewInternalPort=port,
                        NewInternalClient=local_ip,
                        NewEnabled=1,
                        NewPortMappingDescription=description,
                        NewLeaseDuration=0
                    )
                    print(f"[UPnP] SUCCESS: Device at {location} opened port {port}")
                    return True
                    
            except Exception as e:
                # If one device fails (like a smart bulb with bad XML), just skip it
                continue

        print("[UPnP] Exhausted all devices but could not find a valid router for mapping.")
        return False
        
    except Exception as e:
        print(f"[UPnP] Fatal Error: {e}")
        return False
