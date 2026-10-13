"""Terminal client for the Substance 3D Painter bridge.

This is the other half of `painter_bridge.py`. The plugin opens a TCP port
inside Painter; this script connects to it, sends a request, and prints whatever
the app sends back. It only uses the standard library, so any Python works -
including Painter's own bundled interpreter:

  "C:\\Program Files\\...\\Substance 3D Painter 2023\\resources\\pythonsdk\\python.exe" painter_client.py ...

Examples:
  # Liveness check
  python painter_client.py --action ping

  # Run arbitrary Python inside the live app
  python painter_client.py -c "print(project.name())"

  # Run a whole script file inside the app
  python painter_client.py -f my_job.py

  # Named action: apply the stylized shader
  python painter_client.py --action stylize --type Toon --style Cracks

  # ...with shader parameters
  python painter_client.py --action stylize --type Toon --style Cracks \
      --set style_scale=8 --set saturation=1.3

Use --port (or the PAINTER_BRIDGE_PORT env var) if the plugin uses another port.
"""
import argparse
import json
import os
import socket
import sys

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("PAINTER_BRIDGE_PORT", "7301"))


def send(host, port, message, timeout=120.0):
    """Send one request to the bridge and return the decoded reply."""
    payload = (json.dumps(message) + "\n").encode("utf-8")
    with socket.create_connection((host, port), timeout=10.0) as sock:
        sock.sendall(payload)
        sock.settimeout(timeout)
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    line = data.split(b"\n", 1)[0]
    if not line:
        raise IOError("the bridge closed the connection without replying")
    return json.loads(line.decode("utf-8"))


def build_message(args):
    """Turn parsed CLI arguments into the JSON message the plugin expects."""
    if args.action:
        payload = {"action": args.action}
        if args.action == "stylize":
            extra = {"shader_type": args.shader_type, "style": args.style}
            for item in args.set or []:
                key, _, value = item.partition("=")
                try:
                    extra[key] = float(value)
                except ValueError:
                    extra[key] = value
            payload["args"] = extra
        return payload
    if args.file:
        with open(args.file, "r", encoding="utf-8") as handle:
            return {"code": handle.read()}
    return {"code": args.code}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Send commands to a running Substance 3D Painter bridge.")
    source = parser.add_argument_group("what to run (pick one)")
    source.add_argument("-c", "--code", help="Python to run inside Painter")
    source.add_argument("-f", "--file", help="a .py file to run inside Painter")
    source.add_argument("--action", help="a named bridge action (e.g. ping, "
                                         "project_info, stylize)")
    parser.add_argument("--type", dest="shader_type", default="Toon",
                        help="stylize: shader type name (default Toon)")
    parser.add_argument("--style", default="Cracks",
                        help="stylize: style name (default Cracks)")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="stylize: set a shader parameter (repeatable)")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="bridge host (default %s)" % DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="bridge port (default %d)" % DEFAULT_PORT)
    args = parser.parse_args(argv)

    if not (args.code or args.file or args.action):
        parser.error("give one of -c/--code, -f/--file, or --action")

    message = build_message(args)
    try:
        reply = send(args.host, args.port, message)
    except OSError as error:
        print("Could not reach the Painter bridge on %s:%d."
              % (args.host, args.port))
        print("Open Painter with the 'painter_bridge' plugin enabled, then retry.")
        print("Details: %s" % error)
        return 1

    output = reply.get("stdout") or ""
    if output:
        sys.stdout.write(output)
        if not output.endswith("\n"):
            sys.stdout.write("\n")
    if reply.get("result") is not None:
        print(reply["result"])
    if not reply.get("ok"):
        print("ERROR: %s" % reply.get("error"))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
