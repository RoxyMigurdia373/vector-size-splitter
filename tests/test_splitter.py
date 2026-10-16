import io
import zipfile
from decimal import Decimal
from pathlib import Path

import fiona
import pytest
from pyproj import CRS

from io_utils import (Source, VectorError, directory_size, dumps, loads,
                      pack_outputs, prepare_input, same_value, size_bytes)
from splitter import split_vector


def feature(i=0, properties=None, geometry=None):
    return {"type": "Feature", "id": f"ID-{i}",
            "properties": properties if properties is not None else {"uid": i, "name": "中文" * 20},
            "geometry": geometry or {"type": "Point", "coordinates": [100 + i * 0.001, 20]}}


def source_file(tmp_path, features, crs=None):
    path = tmp_path / "source.geojson"
    obj = {"type": "FeatureCollection", "features": features}
    if crs:
        obj["crs"] = {"type": "name", "properties": {"name": crs}}
    path.write_text(dumps(obj), encoding="utf-8")
    return path


def read_parts(result):
    records = []
    for part in result["parts"]:
        records.extend(Source(part["path"]).features())
    return records


def test_geojson_exact_limits_and_attributes(tmp_path):
    features = [feature(i, {"long_field_name": "中文" * 20, "uid": 2**60 + i,
                            "nested": {"a": [1, None, True]}, "price": Decimal("0.123456789012345678901"),
                            "float": Decimal("1.0"), "empty": ""}) for i in range(80)]
    result = split_vector(source_file(tmp_path, features), tmp_path / "out", 1500)
    assert result["input_count"] == result["output_count"] == 80
    assert len(result["parts"]) > 1
    actual = read_parts(result)
    assert all(same_value(a, b) for a, b in zip(features, actual))
    assert all(Path(p["path"]).stat().st_size == p["size_bytes"] <= 1500 for p in result["parts"])
    assert not (tmp_path / "out" / "spool.sqlite").exists()


@pytest.mark.parametrize("fmt,limit", [("shp", 1800), ("gpkg", 81920)])
def test_native_size_attributes_and_roundtrip(tmp_path, fmt, limit):
    original = [feature(i) for i in range(150)]
    result = split_vector(source_file(tmp_path, original), tmp_path / "out", limit, fmt)
    assert result["output_count"] == 150
    assert len(result["parts"]) > 1
    assert [x["properties"] for x in read_parts(result)] == [x["properties"] for x in original]
    for p in result["parts"]:
        assert directory_size(Path(p["path"]).parent) == p["size_bytes"] <= limit
        if fmt == "shp":
            assert {p.suffix for p in Path(p["path"]).parent.iterdir()} >= {".shp", ".shx", ".dbf", ".prj", ".cpg"}
    # 原生输入再次切分，覆盖 SHP/GPKG 输入路径及 schema 保留。
    again = split_vector(result["parts"][0]["path"], tmp_path / "again", limit, fmt)
    assert again["output_count"] == result["parts"][0]["feature_count"]


@pytest.mark.parametrize("fmt", ["geojson", "shp", "gpkg"])
def test_single_feature_oversize(tmp_path, fmt):
    result = split_vector(source_file(tmp_path, [feature(0), feature(1)]), tmp_path / "out", 10, fmt)
    assert len(result["parts"]) == 2
    assert all(p["oversized"] and p["feature_count"] == 1 for p in result["parts"])
    assert len(result["log"]) == 2


def test_invalid_empty_and_repair(tmp_path):
    empty = feature(1)
    empty["geometry"] = None
    bowtie = feature(2, geometry={"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [0, 2], [2, 0], [0, 0]]]})
    result = split_vector(source_file(tmp_path, [feature(), empty, bowtie]), tmp_path / "out", 2000)
    assert (result["input_count"], result["output_count"], result["skipped_count"], result["repaired_count"]) == (3, 2, 1, 1)


def test_aux_fields_and_double_digit_parts(tmp_path):
    result = split_vector(source_file(tmp_path, [feature(i) for i in range(15)]), tmp_path / "out", 400, add_fields=True)
    for index, part in enumerate(result["parts"], 1):
        for record in Source(part["path"]).features():
            assert record["properties"]["part_id"] == index
            assert record["properties"]["orig_fid"] == record["id"]
    assert len(result["parts"]) >= 10


def test_reject_field_collision(tmp_path):
    with pytest.raises(VectorError, match="同名"):
        split_vector(source_file(tmp_path, [feature(properties={"part_id": 1})]), tmp_path / "out", 1000, add_fields=True)


@pytest.mark.parametrize("props", [{"very_long_field_name": 1}, {"name": "汉" * 150}])
def test_shapefile_reject_loss(tmp_path, props):
    with pytest.raises(VectorError):
        split_vector(source_file(tmp_path, [feature(properties=props)]), tmp_path / "out", 10000, "shp")


def test_gpkg_fid_property_preserved(tmp_path):
    records = [feature(i, {"fid": 10, "__split_fid": i, "name": "a"}) for i in range(3)]
    result = split_vector(source_file(tmp_path, records), tmp_path / "out", 200000, "gpkg")
    assert [f["properties"] for f in read_parts(result)] == [f["properties"] for f in records]


def test_transform_and_preserve_crs(tmp_path):
    path = source_file(tmp_path, [feature(geometry={"type": "Point", "coordinates": [1113194.9079327357, 0]})], "EPSG:3857")
    result = split_vector(path, tmp_path / "keep", 1000)
    assert Source(result["parts"][0]["path"]).crs.equals(CRS.from_epsg(3857))
    result = split_vector(path, tmp_path / "wgs", 1000, wgs84=True)
    record = read_parts(result)[0]
    assert float(record["geometry"]["coordinates"][0]) == pytest.approx(10)
    assert Source(result["parts"][0]["path"]).crs.equals(CRS.from_epsg(4326))


@pytest.mark.parametrize("geometry", [
    {"type": "MultiPoint", "coordinates": [[1, 1], [2, 2]]},
    {"type": "LineString", "coordinates": [[1, 1], [2, 2]]},
    {"type": "MultiLineString", "coordinates": [[[1, 1], [2, 2]]]},
    {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
    {"type": "MultiPolygon", "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]]]},
    {"type": "GeometryCollection", "geometries": [{"type": "Point", "coordinates": [1, 1]}]},
])
def test_geometry_types(tmp_path, geometry):
    result = split_vector(source_file(tmp_path, [feature(geometry=geometry)]), tmp_path / "out", 1000)
    assert read_parts(result)[0]["geometry"] == geometry


def test_pack_shapefile_and_upload(tmp_path):
    result = split_vector(source_file(tmp_path, [feature()]), tmp_path / "out", 2000, "shp")
    archive = tmp_path / "all.zip"
    pack_outputs(result["parts"], archive, {"output_count": 1})
    with zipfile.ZipFile(archive) as z:
        assert "manifest.json" in z.namelist()
        nested = next(n for n in z.namelist() if n.endswith(".zip"))
        upload = io.BytesIO(z.read(nested))
        upload.name = "shape.zip"
    paths = prepare_input(upload, tmp_path / "input")
    assert len(paths) == 1
    assert len(list(Source(paths[0]).features())) == 1


@pytest.mark.parametrize("entry", ["../evil.shp", "C:/evil.shp", "..\\evil.shp"])
def test_zip_traversal(tmp_path, entry):
    upload = io.BytesIO()
    upload.name = "bad.zip"
    with zipfile.ZipFile(upload, "w") as z:
        z.writestr(entry, "bad")
    with pytest.raises(VectorError, match="路径"):
        prepare_input(upload, tmp_path / "input")


def test_empty_and_missing_shp(tmp_path):
    upload = io.BytesIO()
    upload.name = "empty.json"
    with pytest.raises(VectorError, match="为空"):
        prepare_input(upload, tmp_path / "empty")
    upload = io.BytesIO()
    upload.name = "bad.zip"
    with zipfile.ZipFile(upload, "w") as z:
        z.writestr("readme.txt", "test")
    with pytest.raises(VectorError, match="未找到"):
        prepare_input(upload, tmp_path / "bad")


@pytest.mark.parametrize("value,unit", [(0, "MB"), (-1, "KB"), (1, "TB"), ("NaN", "MB")])
def test_invalid_limits(value, unit):
    with pytest.raises(VectorError):
        size_bytes(value, unit)


def test_units():
    assert size_bytes(1, "KB") == 1024
    assert size_bytes(10, "MB") == 10485760
    assert size_bytes(0.5, "GB") == 536870912


def test_empty_features(tmp_path):
    with pytest.raises(VectorError, match="没有要素"):
        split_vector(source_file(tmp_path, []), tmp_path / "out", 1000)


def test_streamlit_startup():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
    assert not app.exception
    assert app.title[0].value == "按文件大小切分矢量数据"


def test_streamlit_upload_split_download(monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    upload = io.BytesIO(dumps({"type": "FeatureCollection", "features": [feature(i) for i in range(5)]}).encode("utf-8"))
    upload.name = "example.geojson"
    upload.size = len(upload.getvalue())
    upload.file_id = "test-upload"
    monkeypatch.setattr(st, "file_uploader", lambda *a, **kw: upload)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
    app.button[0].click().run()
    assert not app.exception
    assert not app.error
    assert app.success
    result = app.session_state["split_result"]
    assert result["manifest"]["output_count"] == 5
    with zipfile.ZipFile(io.BytesIO(result["data"])) as archive:
        assert "example_part001.geojson" in archive.namelist()
        assert loads(archive.read("manifest.json"))["output_count"] == 5


def test_boolean_gpkg_and_shapefile_rejection(tmp_path):
    path = source_file(tmp_path, [feature(properties={"enabled": True})])
    result = split_vector(path, tmp_path / "gpkg", 200000, "gpkg")
    assert read_parts(result)[0]["properties"]["enabled"] is True
    with pytest.raises(VectorError):
        split_vector(path, tmp_path / "shp", 200000, "shp")


def test_missing_crs_rejects_reprojection(tmp_path):
    path = tmp_path / "no_crs.shp"
    with fiona.open(path, "w", driver="ESRI Shapefile", schema={"geometry": "Point", "properties": {"uid": "int"}}) as target:
        target.write({"geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": {"uid": 1}})
    with pytest.raises(VectorError, match="坐标系"):
        split_vector(path, tmp_path / "out", 1000, wgs84=True)


def test_native_precision_loss_is_rejected(tmp_path):
    path = source_file(tmp_path, [feature(properties={"value": Decimal("0.12345678901234567890123")})])
    with pytest.raises(VectorError, match="属性"):
        split_vector(path, tmp_path / "out", 200000, "gpkg")


def face_features():
    # 第一个面包含内洞与 Z 坐标；第二个面与它不相交。
    polygon = [
        [[0, 0, 7], [4, 0, 7], [4, 4, 7], [0, 4, 7], [0, 0, 7]],
        [[1, 1, 7], [1, 2, 7], [2, 2, 7], [2, 1, 7], [1, 1, 7]],
    ]
    second = [[[6, 0, 7], [8, 0, 7], [8, 2, 7], [6, 2, 7], [6, 0, 7]]]
    props = {"uid": 2**60, "nested": {"x": [True, None]}, "value": Decimal("0.123456789012345678901")}
    return [feature(1, props, {"type": "Polygon", "coordinates": polygon}),
            feature(2, props, {"type": "MultiPolygon", "coordinates": [polygon, second]})]


def test_face_multipolygon_preserves_properties_coordinates_and_count(tmp_path):
    original = face_features()
    result = split_vector(source_file(tmp_path, original), tmp_path / "out", 1000, geometry_type="MultiPolygon")
    records = read_parts(result)
    assert result["output_count"] == result["input_count"] == 2
    assert all(r["geometry"]["type"] == "MultiPolygon" for r in records)
    assert records[0]["geometry"]["coordinates"] == [original[0]["geometry"]["coordinates"]]
    assert same_value(records[1], original[1])
    assert same_value(records[0]["properties"], original[0]["properties"])
    assert all(p["size_bytes"] <= 1000 for p in result["parts"])


def test_face_polygon_single_part_keeps_attributes_holes_and_bbox(tmp_path):
    original = face_features()
    original[1]["geometry"]["coordinates"] = original[1]["geometry"]["coordinates"][:1]
    original[1]["bbox"] = [0, 0, 4, 4]
    result = split_vector(source_file(tmp_path, original), tmp_path / "out", 600,
                          geometry_type="Polygon")
    records = read_parts(result)
    assert result["input_count"] == result["output_count"] == 2
    assert all(r["geometry"]["type"] == "Polygon" for r in records)
    assert [r["geometry"]["coordinates"] for r in records] == [original[0]["geometry"]["coordinates"], *original[1]["geometry"]["coordinates"]]
    assert all(same_value(r["properties"], original[0]["properties"]) for r in records)
    assert records[1]["id"] == original[1]["id"]
    assert records[1]["bbox"] == original[1]["bbox"]
    assert all(p["size_bytes"] <= 600 for p in result["parts"])


def test_face_polygon_strict_and_single_part_unwrap(tmp_path):
    original = face_features()
    path = source_file(tmp_path, original)
    with pytest.raises(VectorError, match="多个面"):
        split_vector(path, tmp_path / "strict", 2000, geometry_type="Polygon")
    assert not list((tmp_path / "strict").glob("*.geojson"))
    original[1]["geometry"]["coordinates"] = original[1]["geometry"]["coordinates"][:1]
    result = split_vector(source_file(tmp_path, original), tmp_path / "single", 2000, geometry_type="Polygon")
    assert result["output_count"] == 2
    assert all(f["geometry"]["type"] == "Polygon" for f in read_parts(result))


@pytest.mark.parametrize("target", ["Polygon", "MultiPolygon"])
def test_face_non_polygon_is_error_not_silent_skip(tmp_path, target):
    with pytest.raises(VectorError, match="Point"):
        split_vector(source_file(tmp_path, [feature()]), tmp_path / "out", 1000, geometry_type=target)


def test_face_repaired_polygon_converts_after_repair(tmp_path):
    bowtie = feature(geometry={"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [0, 2], [2, 0], [0, 0]]]})
    result = split_vector(source_file(tmp_path, [bowtie]), tmp_path / "out", 1000,
                          geometry_type="MultiPolygon", add_fields=True)
    records = read_parts(result)
    assert result["repaired_count"] == 1
    assert result["output_count"] == 1
    assert all(r["geometry"]["type"] == "MultiPolygon" for r in records)
    assert all(r["properties"]["orig_fid"] == bowtie["id"] for r in records)
    with pytest.raises(VectorError, match="多个面"):
        split_vector(tmp_path / "source.geojson", tmp_path / "strict", 1000, geometry_type="Polygon")


def test_face_default_keeps_geometry_types(tmp_path):
    original = face_features()
    result = split_vector(source_file(tmp_path, original), tmp_path / "out", 2000)
    assert same_value(read_parts(result), original)


def test_streamlit_geometry_choice_and_strict_error(monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    upload = io.BytesIO(dumps({"type": "FeatureCollection", "features": face_features()}).encode("utf-8"))
    upload.name = "faces.geojson"
    upload.size = len(upload.getvalue())
    upload.file_id = "face-upload"
    monkeypatch.setattr(st, "file_uploader", lambda *a, **kw: upload)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
    select = next(s for s in app.selectbox if s.label == "GeoJSON 几何类型")
    select.select("MultiPolygon").run()
    app.button[0].click().run()
    assert not app.exception
    assert app.session_state["split_result"]["manifest"]["geometry_type"] == "MultiPolygon"
    next(s for s in app.selectbox if s.label == "GeoJSON 几何类型").select("Polygon").run()
    assert "split_result" not in app.session_state
    app.button[0].click().run()
    assert app.error and "多个面" in app.error[0].value
    assert "split_result" not in app.session_state
