import hashlib
import argparse
import json
import os
import urllib.request
from pathlib import Path

parser=argparse.ArgumentParser(description='Download a checksum-verified cloudflared Linux binary.')
parser.add_argument('--arch',choices=['arm64','amd64'],required=True)
parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'bin/cloudflared')
args=parser.parse_args()
request=urllib.request.Request('https://api.github.com/repos/cloudflare/cloudflared/releases/latest',headers={'User-Agent':'HiveMind-setup'})
release=json.load(urllib.request.urlopen(request))
asset=next(a for a in release['assets'] if a['name']=='cloudflared-linux-'+args.arch)
expected=asset.get('digest','')
if not expected.startswith('sha256:'):
    raise RuntimeError('Release did not provide a SHA256 digest; verify manually before installing.')
data=urllib.request.urlopen(asset['browser_download_url']).read()
if hashlib.sha256(data).hexdigest()!=expected.split(':',1)[1]:
    raise RuntimeError('Release checksum mismatch')
target=args.output
target.parent.mkdir(parents=True,exist_ok=True)
target.write_bytes(data)
target.chmod(0o700)
print('Verified cloudflared '+release['tag_name'])
