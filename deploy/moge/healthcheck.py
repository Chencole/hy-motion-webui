"""Check auth and local health without printing or sending cloud credentials."""
import base64
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('url', nargs='?', default='http://127.0.0.1:18010/api/health')
parser.add_argument('--login-stdin', action='store_true', help='Read the local saved login via an encrypted SSH stdin pipe')
arguments = parser.parse_args()
url = arguments.url
login_text = sys.stdin.read() if arguments.login_stdin else Path('/etc/hymotion-webui/login.txt').read_text()
credentials = dict(line.split('=', 1) for line in login_text.splitlines() if '=' in line)
authorization = base64.b64encode((credentials['username'] + ':' + credentials['password']).encode()).decode()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for attempt in range(20):
    try:
        try:
            opener.open(url, timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 401, 'Unauthenticated response is not 401'
        else:
            raise AssertionError('Endpoint is not protected by authentication')
        request = urllib.request.Request(url, headers={'Authorization': 'Basic ' + authorization})
        with opener.open(request, timeout=5) as response:
            payload = json.load(response)
            assert response.status == 200
        print(json.dumps({'authenticated_health_status': 200, 'unauthenticated_status': 401}))
        break
    except (OSError, AssertionError):
        if attempt == 19:
            raise SystemExit('Health/auth check failed; credentials were not printed.')
        time.sleep(0.5)
