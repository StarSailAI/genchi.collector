#!/usr/bin/env python3
"""Run inside the remote worker; private operator bridge for browser verification."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
from pathlib import Path

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'list', 'claim', 'observe', 'pointer', 'resume', 'cancel'))
    parser.add_argument('--run')
    parser.add_argument('--section', choices=('comic', 'music'), default='comic')
    parser.add_argument('--input', help='Pointer JSON from the latest observation')
    parser.add_argument('--request-id', help='Reuse only to recover a lost reply to the identical pointer command')
    args = parser.parse_args()
    root = Path('/tmp/genchi-browser-verification')
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    headers = {'Authorization': 'Bearer ' + os.environ['BROWSER_API_TOKEN']}
    base = os.environ.get('BROWSER_URL', 'http://browser:3003').rstrip('/')
    if args.action == 'start':
        response = requests.post(base+'/fetch', headers=headers, json={'url': f'https://natalie.mu/{args.section}', 'waitSeconds': 3}, timeout=110)
    elif args.action == 'list':
        response = requests.get(base+'/verification', headers=headers, timeout=15)
    else:
        if not args.run or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', args.run):
            parser.error('--run must be a returned verification id')
        control = root / (args.run+'.json')
        if args.action != 'claim':
            headers['X-Verification-Control'] = json.loads(control.read_text())['controlToken']
        data = {}
        if args.action == 'pointer':
            data = {'requestId': args.request_id or secrets.token_urlsafe(18), 'input': json.loads(args.input or '{}')}
        response = requests.post(f'{base}/verification/{args.run}/{args.action}', headers=headers, json=data, timeout=30)
    if response.headers.get('Content-Type', '').startswith('text/html'):
        path = root / ((args.run or 'initial')+'.html')
        path.write_text(response.text)
        path.chmod(0o600)
        print(json.dumps({'status': response.status_code, 'htmlPath': str(path), 'bytes': len(response.content)}))
        return
    result = response.json()
    if 'controlToken' in result:
        token = result.pop('controlToken')
        control.write_text(json.dumps({'controlToken': token}))
        control.chmod(0o600)
    if 'image' in result:
        path = root / (args.run+'.jpg')
        path.write_bytes(base64.b64decode(result.pop('image')))
        path.chmod(0o600)
        result['imagePath'] = str(path)
    print(json.dumps({'httpStatus': response.status_code, **result}, ensure_ascii=False))


if __name__ == '__main__':
    main()
