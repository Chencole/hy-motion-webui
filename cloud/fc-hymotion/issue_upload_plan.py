"""CloudShell Python 3.6+: issue scoped OSS upload URLs; never upload or invoke FC.

Requires the existing oss2 package and existing ALIBABA_CLOUD_* environment credentials.
Output is private and temporary. Never paste the JSON into public logs or the frontend.
"""
import argparse
import json
import os
import re
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--region', default='cn-hangzhou')
    parser.add_argument('--expires', type=int, default=3600)
    parser.add_argument('--output', default='hymotion-signed-upload-plan.json')
    args = parser.parse_args()
    try:
        import oss2
        if (not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', args.bucket)
                or not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)+', args.region)
                or not 300 <= args.expires <= 3600):
            raise ValueError('Invalid bucket, region or expiry')
        with open(args.manifest, 'r') as stream:
            manifest = json.load(stream)
        access_id = os.environ.get('ALIBABA_CLOUD_ACCESS_KEY_ID')
        access_secret = os.environ.get('ALIBABA_CLOUD_ACCESS_KEY_SECRET')
        token = os.environ.get('ALIBABA_CLOUD_SECURITY_TOKEN')
        if not access_id or not access_secret:
            raise ValueError('Existing CloudShell environment credentials are missing')
        auth = oss2.StsAuth(access_id, access_secret, token) if token else oss2.Auth(access_id, access_secret)
        bucket = oss2.Bucket(auth, 'https://oss-{}.aliyuncs.com'.format(args.region), args.bucket)
        entries, seen = [], set()
        for item in manifest['files']:
            key = item['object_key']
            if (not key.startswith('assets/hymotion/ckpts/') or key in seen
                    or any(part in ('', '.', '..') or not re.fullmatch(r'[A-Za-z0-9._-]+', part) for part in key.split('/'))
                    or not re.fullmatch('[a-f0-9]{64}', item['sha256'])
                    or type(item['size_bytes']) is not int or not 0 < item['size_bytes'] < 5000000000):
                raise ValueError('Invalid asset manifest')
            headers = {'Content-Type': 'application/octet-stream', 'x-oss-meta-sha256': item['sha256'],
                       'x-oss-forbid-overwrite': 'true'}
            entries.append({'object_key': key, 'headers': headers,
                            'put_url': bucket.sign_url('PUT', key, args.expires, headers=headers, slash_safe=True),
                            'head_url': bucket.sign_url('HEAD', key, args.expires, slash_safe=True)})
            seen.add(key)
        if len(entries) != manifest['file_count']:
            raise ValueError('Asset count differs from manifest')
        plan = {'bucket': args.bucket, 'region': args.region, 'expires_at_unix': int(time.time()) + args.expires,
                'files': entries}
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(plan, stream, ensure_ascii=False)
        print('Created a private, temporary plan for {} objects. No cloud API request or upload was sent.'.format(len(entries)))
        return 0
    except Exception:
        print('Unable to issue the upload plan. Check the manifest, existing environment credentials and a new output filename. Credential diagnostics were suppressed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
