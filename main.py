from http.server import BaseHTTPRequestHandler
import requests
import json
from urllib.parse import urlparse, parse_qs

MAIN_API = "https://rootx-osint.in/?type=num&key=seed_bhai&query={q}"

FILTER_KEYS = {"req_left", "req_total", "request left", "request total", "credits", "expiry"}

def filter_response(data):
    if isinstance(data, dict):
        filtered = {k: v for k, v in data.items() if k.lower() not in FILTER_KEYS}
        filtered["developer"] = "@Pankajccc"
        filtered["channel"] = "https://t.me/masterjirayaji"
        return filtered
    return data

# Vercel serverless function entry point
def handler(request):
    path = request.path
    parsed = urlparse(path)
    parts = parsed.path.strip("/").split("/")

    if len(parts) == 2 and parts[0] == "userid":
        query = parts[1]
        try:
            r = requests.get(MAIN_API.format(q=query), timeout=10)
            data = r.json()
            filtered = filter_response(data)
            body = json.dumps(filtered, indent=2)
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": body
            }
        except Exception as e:
            body = json.dumps({"error": str(e)})
            return {
                "statusCode": 500,
                "headers": {"Content-Type": "application/json"},
                "body": body
            }
    else:
        body = json.dumps({"status": "RootX Proxy", "usage": "/userid/<telegram_id>"})
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": body
        }
