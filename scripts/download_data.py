"""Resumable public downloads, hashes and provenance; refuses to exhaust the disk."""
import argparse, hashlib, json, os, shutil, time
from datetime import datetime, timezone
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get('NYC_DATA_DIR', ROOT / 'data')).resolve()
RAW = Path(os.environ.get('NYC_RAW_DIR', DATA_DIR / 'raw')).resolve()
MIN_FREE = 15 * 1024**3

def download(
    url, relative, expected_size=None, *, raw_dir=None, manifests_dir=None,
    max_resident_bytes=None, resident_bytes_fn=None,
):
    raw_dir = Path(raw_dir or RAW).resolve()
    manifests_dir = Path(manifests_dir or raw_dir).resolve()
    target = raw_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = manifests_dir / Path(relative).with_suffix(Path(relative).suffix + '.download.json')
    manifest.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and manifest.exists():
        return target
    part = target.with_name(target.name + '.part')
    for attempt in range(4):
        offset = part.stat().st_size if part.exists() else 0
        try:
            headers = {'User-Agent':'NYC-personal-map-data-research/1.0', 'Accept-Encoding':'identity'}
            if offset:
                headers['Range'] = f'bytes={offset}-'
            with requests.get(url, headers=headers, stream=True, timeout=(30,120)) as r:
                r.raise_for_status()
                if r.status_code != 206:
                    offset = 0
                total = int(r.headers.get('Content-Length', 0)) + offset
                if total and max_resident_bytes is not None and resident_bytes_fn is not None:
                    resident = resident_bytes_fn(target)
                    if resident + total > max_resident_bytes:
                        raise RuntimeError(
                            f'LAZ resident cap exceeded: {resident + total:,} bytes '
                            f'(limit {max_resident_bytes:,})'
                        )
                if total and shutil.disk_usage(raw_dir).free - (total-offset) < MIN_FREE:
                    raise RuntimeError(f'Insufficient storage: {total:,} bytes; preserving 15 GiB free')
                print(f'Download {relative}: {total:,} bytes, resuming at {offset:,}', flush=True)
                checkpoint = time.monotonic()
                with part.open('ab' if offset else 'wb') as f:
                    for chunk in r.iter_content(4*1024**2):
                        if shutil.disk_usage(raw_dir).free < MIN_FREE:
                            raise RuntimeError('Storage reserve reached')
                        f.write(chunk)
                        if time.monotonic() - checkpoint > 25:
                            print(f'  {relative}: {f.tell():,} bytes', flush=True)
                            checkpoint = time.monotonic()
                assert not total or part.stat().st_size == total, 'Truncated response'
                if expected_size is not None:
                    assert part.stat().st_size == expected_size
                h = hashlib.sha256()
                with part.open('rb') as f:
                    for block in iter(lambda:f.read(8*1024**2),b''):
                        h.update(block)
                part.replace(target)
                manifest.write_text(json.dumps({'source_url':url,'resolved_url':r.url,'file':str(target),
                    'bytes':target.stat().st_size,'sha256':h.hexdigest(),'retrieved_at':datetime.now(timezone.utc).isoformat(),
                    'etag':r.headers.get('ETag'),'last_modified':r.headers.get('Last-Modified')},indent=2))
                print(f'Complete {relative}', flush=True)
                return target
        except Exception as e:
            print(f'{relative} attempt {attempt+1}: {e}', flush=True)
            if isinstance(e, RuntimeError) or attempt == 3:
                raise
            time.sleep(2**attempt)

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('url');p.add_argument('path')
    a=p.parse_args();download(a.url,a.path)
