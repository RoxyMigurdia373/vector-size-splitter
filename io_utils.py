"""磁盘 IO、安全解压、流式读取与可验证的矢量写入。"""
from __future__ import annotations

import copy
import re
import shutil
import zipfile
from decimal import Decimal
from pathlib import Path

import fiona
import ijson
import simplejson as json
from fiona.model import to_dict
from pyproj import CRS


class VectorError(ValueError):
    """可直接展示给用户的数据错误。"""


def dumps(value):
    return json.dumps(value, ensure_ascii=False, use_decimal=True,
                      allow_nan=False, separators=(",", ":"))


def loads(value):
    return json.loads(value, use_decimal=True)


def size_bytes(value, unit):
    factors = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}
    if unit not in factors:
        raise VectorError("大小单位必须为 KB、MB 或 GB。")
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise VectorError("最大文件大小必须是有限正数。")
    result = int(number * factors[unit])
    if result < 1:
        raise VectorError("最大文件大小不能小于 1 字节。")
    return result


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name).stem).strip(" .")
    return (name[:100] or "vector")


def prepare_input(upload, folder: Path, max_unpacked=4 * 1024**3):
    """落盘上传文件；限制解压体积、条目数，并阻止路径穿越。"""
    folder.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.name).suffix.lower()
    if suffix not in {".zip", ".geojson", ".json", ".gpkg", ".shp"}:
        raise VectorError("不支持的格式，请上传 zip、geojson、json 或 gpkg。")
    if suffix == ".shp":
        raise VectorError("单个 .shp 缺少属性和索引，请将 .shp/.shx/.dbf 等一起打包为 zip。")
    target = folder / ("upload" + suffix)
    upload.seek(0)
    with target.open("wb") as dest:
        shutil.copyfileobj(upload, dest, length=1024 * 1024)
    if not target.stat().st_size:
        raise VectorError("上传文件为空。")
    if suffix != ".zip":
        return [target]
    extracted = folder / "extracted"
    extracted.mkdir()
    with zipfile.ZipFile(target) as archive:
        entries = archive.infolist()
        if len(entries) > 20000 or sum(x.file_size for x in entries) > max_unpacked:
            raise VectorError("压缩包解压后超过 4GB 或包含超过 20000 个条目，请使用 Docker 调整限制。")
        for entry in entries:
            relative = Path(entry.filename.replace("\\", "/"))
            destination = (extracted / relative).resolve()
            if not destination.is_relative_to(extracted.resolve()) or ":" in entry.filename:
                raise VectorError("压缩包包含不安全的路径。")
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise VectorError("压缩包不能包含符号链接。")
            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as src, destination.open("wb") as dest:
                    shutil.copyfileobj(src, dest, 1024 * 1024)
    gdbs = [p for p in extracted.rglob("*") if p.is_dir() and p.suffix.lower() == ".gdb"]
    shapes = [p for p in extracted.rglob("*") if p.suffix.lower() == ".shp"
              and not any(gdb in p.parents for gdb in gdbs)]
    for shp in shapes:
        siblings = {p.name.lower() for p in shp.parent.iterdir()}
        if any(shp.with_suffix(ext).name.lower() not in siblings for ext in (".dbf", ".shx")):
            raise VectorError(f"{shp.name} 缺少 .dbf 或 .shx 文件。")
    candidates = sorted(gdbs + shapes)
    if not candidates:
        raise VectorError("zip 内未找到 .shp 文件或 .gdb 目录。")
    return candidates


class Source:
    def __init__(self, path, layer=None):
        self.path = Path(path)
        self.layer = layer
        self.raw_json = self.path.suffix.lower() in {".json", ".geojson"}
        self.schema = None
        self.crs = None
        if self.raw_json:
            with self.path.open("rb") as f:
                kind = next(ijson.items(f, "type"), None)
            if kind != "FeatureCollection":
                raise VectorError("GeoJSON 须为 FeatureCollection。")
            with self.path.open("rb") as f:
                crs = next(ijson.items(f, "crs"), None)
            if crs:
                try:
                    self.crs = CRS.from_user_input(crs["properties"]["name"])
                except Exception as exc:
                    raise VectorError("无法识别 GeoJSON CRS。") from exc
            else:
                self.crs = CRS.from_epsg(4326)
        else:
            with fiona.open(self.path, layer=layer) as src:
                self.schema = copy.deepcopy(src.schema)
                self.crs = CRS.from_user_input(src.crs_wkt) if src.crs_wkt else None

    def features(self):
        if self.raw_json:
            with self.path.open("rb") as f:
                yield from ijson.items(f, "features.item")
        else:
            with fiona.open(self.path, layer=self.layer) as src:
                for feature in src:
                    yield to_dict(feature)

    def native_schema(self):
        if self.schema is None:
            with fiona.open(self.path) as src:
                geometry = src.schema["geometry"]
            # GDAL 可能把 GeoJSON 顶层 id 映射成属性；只推断原始 properties。
            properties = {}
            for feature in self.features():
                for name, value in (feature.get("properties") or {}).items():
                    if value is None:
                        properties.setdefault(name, None)
                        continue
                    if isinstance(value, bool):
                        typ = "bool"
                    elif isinstance(value, int):
                        typ = "int64"
                    elif isinstance(value, (Decimal, float)):
                        typ = "float"
                    elif isinstance(value, str):
                        typ = "str"
                    else:
                        raise VectorError(f"字段 {name!r} 是嵌套对象/数组，目标格式无法无损表达，请输出 GeoJSON。")
                    previous = properties.get(name)
                    if previous and previous != typ:
                        raise VectorError(f"字段 {name!r} 包含混合类型，请输出 GeoJSON 保留原类型。")
                    properties[name] = typ
            self.schema = {"geometry": geometry,
                           "properties": {k: v or "str" for k, v in properties.items()}}
        return copy.deepcopy(self.schema)


def json_header(crs):
    header = {"type": "FeatureCollection"}
    if crs and not crs.equals(CRS.from_epsg(4326)):
        header["crs"] = {"type": "name", "properties": {"name": crs.to_string()}}
    return (dumps(header)[:-1] + ',"features":[').encode("utf-8")


def check_schema(schema, output_format):
    """拒绝无法表达的字段，绝不自动截断或重命名。"""
    props = schema["properties"]
    if output_format == "shp":
        names = list(props)
        if any(len(n.encode("utf-8")) > 10 for n in names):
            raise VectorError("Shapefile 字段名最多 10 个 UTF-8 字节，请改用 GeoJSON/GeoPackage。")
        if len({n.casefold() for n in names}) != len(names):
            raise VectorError("Shapefile 不支持仅大小写不同的字段名。")
        if not props:
            raise VectorError("Shapefile 会为无属性数据自动增加 FID，无法保持原字段结构。")
        if len(props) > 255:
            raise VectorError("Shapefile 字段数超过 255。")
    allowed = {"str", "int", "int32", "int64", "float", "date"}
    if output_format == "gpkg":
        allowed |= {"datetime", "bool"}
    for name, typ in props.items():
        if typ.split(":")[0] not in allowed:
            raise VectorError(f"目标格式无法无损表达字段 {name!r} 的类型 {typ!r}。")


def same_value(a, b):
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same_value(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same_value(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
        return isinstance(a, int) == isinstance(b, int) and Decimal(str(a)) == Decimal(str(b))
    return type(a) is type(b) and a == b


def write_native(path, records, schema, crs, output_format):
    driver = {"shp": "ESRI Shapefile", "gpkg": "GPKG"}[output_format]
    options = {"encoding": "UTF-8"}
    if output_format == "gpkg":
        # 避免把输入名为 fid 的普通属性吸收为数据库内部主键。
        names = {n.casefold() for n in schema["properties"]}
        internal = "__split_fid"
        while internal.casefold() in names:
            internal += "_"
        geom_name = "__split_geom"
        while geom_name.casefold() in names:
            geom_name += "_"
        options.update(FID=internal, GEOMETRY_NAME=geom_name, SPATIAL_INDEX="NO", layer="features")
    with fiona.open(path, "w", driver=driver, schema=schema,
                    crs_wkt=crs.to_wkt() if crs else None, **options) as dst:
        for feature in records():
            # 通过 JSON 转为 Fiona 支持的原生数字；随后逐字段回读比较。
            native = json.loads(dumps(feature), use_decimal=False)
            dst.write({"type": "Feature", "geometry": native["geometry"],
                       "properties": native["properties"]})
    with fiona.open(path) as check:
        if list(check.schema["properties"]) != list(schema["properties"]):
            raise VectorError("驱动修改了字段名或字段顺序，已中止导出。")
        def family(typ):
            base = typ.split(":")[0]
            return "int" if base in {"int", "int32", "int64"} else base
        if any(family(check.schema["properties"][key]) != family(typ)
               for key, typ in schema["properties"].items()):
            raise VectorError("驱动改变了属性字段类型，已中止导出。")
        expected = iter(records())
        count = 0
        for actual in check:
            original = next(expected, None)
            if original is None or not same_value(original["properties"], dict(actual["properties"])):
                raise VectorError("回读发现属性值/类型变化（可能是 DBF 截断、精度或空值限制），请改用 GeoJSON。")
            count += 1
        if next(expected, None) is not None:
            raise VectorError("输出要素数不一致。")
    return count


def directory_size(folder):
    return sum(p.stat().st_size for p in Path(folder).rglob("*") if p.is_file())


def pack_outputs(parts, destination, manifest):
    """Shapefile 每个分片单独 zip，再与清单一起放入总 zip。"""
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for part in parts:
            path = Path(part["path"])
            if path.suffix == ".shp":
                nested = path.parent.parent / (path.stem + ".zip")
                with zipfile.ZipFile(nested, "w", zipfile.ZIP_DEFLATED) as shpzip:
                    for component in sorted(path.parent.iterdir()):
                        shpzip.write(component, component.name)
                archive.write(nested, nested.name)
            else:
                archive.write(path, path.name)
        archive.writestr("manifest.json", dumps(manifest).encode("utf-8"))
