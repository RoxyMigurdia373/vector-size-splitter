"""显式运行 100MiB 验收：python tests/benchmark_100mb.py。"""
import hashlib
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from io_utils import Source, dumps
from splitter import split_vector


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    source = root / "large.geojson"
    count = 0
    expected = hashlib.sha256()
    with source.open("wb") as stream:
        stream.write(b'{"type":"FeatureCollection","features":[')
        while stream.tell() < 100 * 1024**2:
            obj = {"type": "Feature", "properties": {"uid": count, "text": "中ABC" * 1365},
                   "geometry": {"type": "Point", "coordinates": [100, 20]}}
            payload = dumps(obj).encode("utf-8")
            expected.update(payload)
            if count:
                stream.write(b",")
            stream.write(payload)
            count += 1
        stream.write(b"]}")
    started = time.monotonic()
    result = split_vector(source, root / "out", 10 * 1024**2)
    actual = hashlib.sha256()
    for part in result["parts"]:
        assert Path(part["path"]).stat().st_size <= 10 * 1024**2
        for obj in Source(part["path"]).features():
            actual.update(dumps(obj).encode("utf-8"))
    assert count == result["output_count"]
    assert expected.digest() == actual.digest()
    print(f"PASS: {source.stat().st_size} bytes, {count} features, {len(result['parts'])} parts, {time.monotonic()-started:.2f}s; full feature SHA256 matches")
