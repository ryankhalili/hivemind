"""Check a running server without creating tasks or connecting devices."""
import argparse
import json
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', help='HTTPS server address, or localhost for development')
    parser.add_argument('--username', default='owner')
    parser.add_argument('--password-file', type=Path, required=True)
    parser.add_argument('--downloads', action='store_true', help='Also check built connector downloads')
    args = parser.parse_args()
    url = args.url.rstrip('/')
    with httpx.Client(base_url=url, timeout=30, headers={'Origin': url}) as client:
        assert client.get('/health').json()['ok']
        assert client.get('/api/state').status_code == 401
        response = client.post('/api/login', json={
            'username': args.username, 'password': args.password_file.read_text().strip()})
        assert response.status_code == 200, 'Sign-in failed'
        cookie = response.headers['set-cookie']
        assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie
        if url.startswith('https://'):
            assert 'Secure' in cookie
        state = client.get('/api/state')
        assert state.status_code == 200
        assert 'no-store' in state.headers['cache-control']
        paths = ['/', '/static/style.css', '/static/app.js', '/sw.js',
                 '/static/manifest.webmanifest', '/guide', '/docs/architecture.md']
        if args.downloads:
            paths += ['/downloads/HiveMind-Setup.exe', '/downloads/HiveMind-Mac.zip',
                      '/downloads/HiveMind-Browser.zip']
        for path in paths:
            with client.stream('GET', path) as response:
                assert response.status_code == 200, f'Missing resource: {path}'
        assert client.post('/api/pairing', json={}, headers={
            'Origin': 'https://wrong.example'}).status_code == 403
        assert client.post('/api/logout', json={}).status_code == 200
        assert client.get('/api/state').status_code == 401
    print(json.dumps({'health': 'passed', 'authentication': 'passed',
                      'cookies': 'passed', 'origin_check': 'passed', 'resources': 'passed'}))


if __name__ == '__main__':
    main()
