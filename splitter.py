"""按真实文件大小切分。工作集落在 SQLite，内存只保留单个要素。"""
from __future__ import annotations

import copy
import shutil
import sqlite3
from pathlib import Path

from pyproj import CRS, Transformer
from shapely import make_valid
from shapely.geometry import mapping, shape
from shapely.ops import transform

from io_utils import (Source, VectorError, check_schema, directory_size, dumps,
                      json_header, loads, safe_name, write_native)


def convert_face_geometry(feature, geometry_type):
    """只调整面坐标的嵌套层级，不重算坐标、合并面或修改 properties。"""
    if geometry_type == "keep":
        return feature
    geometry = feature["geometry"]
    current = geometry["type"]
    if current not in {"Polygon", "MultiPolygon"}:
        raise VectorError(f"选择 {geometry_type} 输出，但遇到 {current} 几何。请使用“保持原类型”或上传纯面数据。")
    if geometry_type == "MultiPolygon":
        if current == "MultiPolygon":
            return feature
        return {**feature, "geometry": {**geometry, "type": "MultiPolygon",
                                         "coordinates": [geometry["coordinates"]]}}
    if current == "Polygon":
        return feature
    polygons = geometry["coordinates"]
    if len(polygons) != 1:
        raise VectorError("遇到包含多个面的 MultiPolygon，不能在不拆分的情况下转换为 Polygon。请输出 MultiPolygon 或选择“保持原类型”。")
    return {**feature, "geometry": {**geometry, "type": "Polygon", "coordinates": polygons[0]}}


def split_vector(input_path, output_dir, max_bytes, output_format="geojson",
                 *, layer=None, base_name=None, add_fields=False, wgs84=False,
                 progress=None, geometry_type="keep"):
    if output_format not in {"geojson", "shp", "gpkg"}:
        raise VectorError("输出格式必须为 geojson、shp 或 gpkg。")
    if geometry_type not in {"keep", "Polygon", "MultiPolygon"}:
        raise VectorError("几何类型必须为 keep、Polygon 或 MultiPolygon。")
    if geometry_type != "keep" and output_format != "geojson":
        raise VectorError("指定 Polygon/MultiPolygon 仅适用于 GeoJSON 输出。")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise VectorError("大小阈值必须是正整数字节数。")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise VectorError("输出目录必须为空。")
    source = Source(input_path, layer)
    if output_format == "geojson" and source.crs is None:
        raise VectorError("输入没有坐标系；GeoJSON 默认代表 WGS84，无法无损保留未知 CRS，请选择原生格式。")
    crs = CRS.from_epsg(4326) if wgs84 else source.crs
    if wgs84 and source.crs is None:
        raise VectorError("输入数据缺少坐标系，无法转换为 WGS84。")
    transformer = Transformer.from_crs(source.crs, crs, always_xy=True) if wgs84 else None
    schema = source.native_schema() if output_format != "geojson" else copy.deepcopy(source.schema)
    if output_format == "geojson" and schema:
        for field, typ in schema["properties"].items():
            if typ.split(":")[0] in {"date", "datetime", "time", "bytes"}:
                raise VectorError(f"GeoJSON 无法保留字段 {field} 的原生类型 {typ}，请输出 GeoPackage。")
    if add_fields and schema:
        if {"orig_fid", "part_id"} & schema["properties"].keys():
            raise VectorError("输入已有 orig_fid 或 part_id，无法添加同名辅助字段。")
        schema["properties"].update(orig_fid="str:254", part_id="int")
    if output_format != "geojson":
        check_schema(schema, output_format)
    stem = safe_name(base_name or Path(input_path).stem)
    stats = {"input_count": 0, "output_count": 0, "skipped_count": 0,
             "repaired_count": 0, "max_bytes": max_bytes, "format": output_format,
             "geometry_type": geometry_type,
             "log": [], "log_truncated": 0, "parts": []}

    def log(text):
        if len(stats["log"]) < 1000:
            stats["log"].append(text)
        else:
            stats["log_truncated"] += 1

    database = output_dir / "spool.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA cache_size=-4096")
        connection.execute("CREATE TABLE features (n INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        count = 0
        for index, feature in enumerate(source.features(), 1):
            stats["input_count"] += 1
            if not isinstance(feature, dict) or feature.get("type") != "Feature":
                raise VectorError(f"第 {index} 条不是 GeoJSON Feature。")
            properties = feature.get("properties")
            if properties is not None and not isinstance(properties, dict):
                raise VectorError(f"第 {index} 条属性不是对象或 null。")
            if add_fields and properties and {"orig_fid", "part_id"} & properties.keys():
                raise VectorError("输入已有辅助字段同名属性。")
            try:
                geometry = shape(feature["geometry"]) if feature.get("geometry") else None
                if geometry is None or geometry.is_empty:
                    raise ValueError("空几何")
                changed = False
                if not geometry.is_valid:
                    geometry = make_valid(geometry)
                    changed = True
                    stats["repaired_count"] += 1
                    log(f"第 {index} 条几何已 make_valid 修复。")
                if transformer:
                    geometry = transform(lambda x, y, z=None: transformer.transform(x, y, z, errcheck=True), geometry)
                    changed = True
                if geometry.is_empty or not geometry.is_valid:
                    raise ValueError("无法修复的几何")
                if changed:
                    feature["geometry"] = mapping(geometry)
                    feature.pop("bbox", None)
            except Exception as exc:
                stats["skipped_count"] += 1
                log(f"跳过第 {index} 条：{exc}")
                continue
            if output_format != "geojson":
                expected = set(schema["properties"]) - ({"orig_fid", "part_id"} if add_fields else set())
                if properties is None or set(properties) != expected:
                    raise VectorError("目标格式要求固定字段结构；存在 null 属性或各要素字段不一致，请输出 GeoJSON。")
            if add_fields:
                feature["properties"] = dict(properties or {})
                feature["properties"]["orig_fid"] = str(feature.get("id", index - 1))
            try:
                converted = convert_face_geometry(feature, geometry_type)
            except VectorError as exc:
                raise VectorError(f"第 {index} 个要素：{exc}") from exc
            count += 1
            connection.execute("INSERT INTO features VALUES (?, ?)", (count, dumps(converted)))
            if index % 2000 == 0:
                connection.commit()
                if progress:
                    progress(f"已读取 {index:,} 个要素，已跳过 {stats['skipped_count']:,} 个")
        connection.commit()
        if stats["input_count"] == 0:
            raise VectorError("输入文件没有要素。")
        if count == 0:
            raise VectorError("没有可输出的有效几何。" + "；".join(stats["log"][:5]))

        def records(start, end, part_number):
            for (body,) in connection.execute("SELECT body FROM features WHERE n BETWEEN ? AND ? ORDER BY n", (start, end)):
                feature = loads(body)
                if add_fields:
                    feature["properties"]["part_id"] = part_number
                yield feature

        def register(path, size, features):
            oversized = size > max_bytes
            if oversized:
                if features != 1:
                    raise AssertionError("多要素分片超限")
                log(f"{path.name}：单要素及格式开销 {size} 字节超过阈值 {max_bytes}，已单独输出。")
            stats["parts"].append({"path": str(path), "name": path.name, "size_bytes": size,
                                   "feature_count": features, "oversized": oversized})
            stats["output_count"] += features
            if progress:
                progress(f"已生成 {len(stats['parts'])} 个分片，输出 {stats['output_count']:,} 个要素")

        if output_format == "geojson":
            header = json_header(crs)
            number, written, size, stream, path = 0, 0, 0, None, None
            try:
                for n in range(1, count + 1):
                    feature = next(records(n, n, number or 1))
                    payload = dumps(feature).encode("utf-8")
                    if stream and written and size + 1 + len(payload) + 2 > max_bytes:
                        stream.write(b"]}")
                        stream.close()
                        register(path, path.stat().st_size, written)
                        stream = None
                    if stream is None:
                        number += 1
                        if add_fields:
                            feature["properties"]["part_id"] = number
                            payload = dumps(feature).encode("utf-8")
                        path = output_dir / f"{stem}_part{number:03d}.geojson"
                        stream = path.open("wb")
                        stream.write(header)
                        written, size = 0, len(header)
                    if written:
                        stream.write(b",")
                        size += 1
                    stream.write(payload)
                    written += 1
                    size += len(payload)
                if stream:
                    stream.write(b"]}")
                    stream.close()
                    register(path, path.stat().st_size, written)
            finally:
                if stream and not stream.closed:
                    stream.close()
        else:
            # 每次候选写入独立目录并关闭驱动；二分选择最大已验证前缀。
            # SQLite 页、DBF 头及所有 sidecar 均包含在实际体积中。
            start, number, previous = 1, 1, 1
            while start <= count:
                trial = output_dir / "trial"
                def measure(length):
                    if trial.exists():
                        shutil.rmtree(trial)
                    trial.mkdir()
                    name = f"{stem}_part{number:03d}.{output_format}"
                    path = trial / name
                    write_native(path, lambda: records(start, start + length - 1, number),
                                 schema, crs, output_format)
                    return directory_size(trial)

                remaining = count - start + 1
                length = min(previous, remaining)
                size = measure(length)
                if size <= max_bytes:
                    low, high = length, None
                    while low < remaining:
                        candidate = min(low * 2, remaining)
                        if measure(candidate) > max_bytes:
                            high = candidate
                            break
                        low = candidate
                    if high is None:
                        high = low
                else:
                    low, high = 0, length
                while high - low > 1:
                    middle = (low + high) // 2
                    if measure(middle) <= max_bytes:
                        low = middle
                    else:
                        high = middle
                chosen = max(1, low)
                size = measure(chosen)
                destination = output_dir / f"part{number:03d}"
                trial.rename(destination)
                register(destination / f"{stem}_part{number:03d}.{output_format}", size, chosen)
                start += chosen
                previous = chosen
                number += 1
        if len(stats["parts"]) > 1000:
            log("输出超过 1000 个分片，建议增大阈值。")
        assert stats["input_count"] == stats["output_count"] + stats["skipped_count"]
        return stats
    finally:
        connection.close()
        database.unlink(missing_ok=True)
