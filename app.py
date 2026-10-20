"""Streamlit 入口：所有处理文件均在 TemporaryDirectory 中自动清理。"""
import os
import tempfile
from pathlib import Path

import fiona
import streamlit as st

from io_utils import VectorError, pack_outputs, prepare_input, safe_name, size_bytes
from splitter import split_vector


st.set_page_config(page_title="按文件大小切分矢量数据", page_icon="🗺️", layout="wide")
from pathlib import Path as _ThemePath
_theme = _ThemePath(__file__).resolve().parent / "style.css"
if _theme.is_file():
    st.markdown("<style>" + _theme.read_text(encoding="utf-8") + "</style>", unsafe_allow_html=True)
st.title("按文件大小切分矢量数据")
st.caption("按完整要素切分，保留属性；大小以关闭后的实际文件为准。1 MB = 1024² 字节。")

with st.sidebar:
    st.header("切分参数")
    maximum = st.number_input("最大文件大小", min_value=0.001, value=10.0, format="%.3f")
    unit = st.selectbox("单位", ["KB", "MB", "GB"], index=1)
    output_format = st.selectbox("输出格式", ["geojson", "shp", "gpkg"])
    geometry_type = "keep"
    if output_format == "geojson":
        geometry_type = st.selectbox("GeoJSON 几何类型", ["keep", "Polygon", "MultiPolygon"],
                                     format_func=lambda value: "保持原类型" if value == "keep" else value)
        if geometry_type == "Polygon":
            st.caption("只转换单部件面；遇到包含多个面的 MultiPolygon 会报错，不拆分要素。")
        elif geometry_type == "MultiPolygon":
            st.caption("将 Polygon 转为 MultiPolygon，已有 MultiPolygon 保持不变；不改变要素数和属性。")
        if geometry_type != "keep":
            st.caption("指定面类型仅适用于纯面数据；点、线或 GeometryCollection 会提示错误。")
    add_fields = st.checkbox("新增 orig_fid、part_id 辅助字段", value=False)
    wgs84 = st.checkbox("转换为 WGS84（EPSG:4326）", value=False)
    st.caption("Shapefile 阈值是全部组成文件的未压缩总大小；不是 zip 的大小。")
    st.caption("无法修复的几何会跳过并记入清单。单要素超限会单独输出并警告。")

upload = st.file_uploader("上传一个矢量文件（Shapefile / GDB 请打包为 zip）",
                          type=["zip", "geojson", "json", "gpkg", "shp"],
                          accept_multiple_files=False)
if upload is None:
    st.session_state.pop("split_result", None)
    st.info("支持 GeoJSON、Shapefile、GeoPackage，以及当前 GDAL 驱动可读取的 File Geodatabase。")
else:
    st.write(f"文件：{upload.name} · {upload.size / 1024**2:,.2f} MB")
    if upload.size > 100 * 1024**2:
        st.warning("大文件在免费 Cloud 的约 1GB 内存环境中可能失败。上传内容和下载压缩包仍由 Streamlit 驻留内存，建议使用 Docker。")
    try:
        with tempfile.TemporaryDirectory(prefix="vector_split_") as temporary:
            root = Path(temporary)
            candidates = prepare_input(upload, root / "input")
            # 控件值只保存稳定的相对路径，不能保留上次运行的临时目录。
            selected = st.selectbox("数据集", [str(p.relative_to(root / "input")) for p in candidates])
            dataset = root / "input" / selected
            layer = None
            if dataset.suffix.lower() not in {".json", ".geojson"}:
                layers = fiona.listlayers(dataset)
                if not layers:
                    raise VectorError("数据集中没有可读取的图层；GDB 请检查 OpenFileGDB 驱动。")
                layer = st.selectbox("图层（每次切分一个图层）", layers)
            signature = (getattr(upload, "file_id", upload.name), upload.size,
                         str(dataset.relative_to(root)), layer, maximum, unit,
                         output_format, add_fields, wgs84, geometry_type)
            saved = st.session_state.get("split_result")
            if saved and saved["signature"] != signature:
                st.session_state.pop("split_result", None)
            if st.button("开始切分", type="primary"):
                st.session_state.pop("split_result", None)
                status = st.empty()
                with st.spinner("正在读取、切分并验证实际文件大小…"):
                    result = split_vector(dataset, root / "output", size_bytes(maximum, unit),
                                          output_format, layer=layer, base_name=safe_name(upload.name),
                                          add_fields=add_fields, wgs84=wgs84, progress=status.info,
                                          geometry_type=geometry_type)
                    manifest = dict(result)
                    manifest["parts"] = [{k: v for k, v in part.items() if k != "path"}
                                         for part in result["parts"]]
                    archive = root / "download.zip"
                    pack_outputs(result["parts"], archive, manifest)
                    download_limit = int(os.getenv("MAX_DOWNLOAD_MB", "200")) * 1024**2
                    if archive.stat().st_size > download_limit:
                        raise VectorError("下载包超过内存保护限制（默认 200MB）。请在 Docker 中设置 MAX_DOWNLOAD_MB 并分配足够内存后重试。")
                    st.session_state["split_result"] = {
                        "signature": signature, "manifest": manifest,
                        "data": archive.read_bytes(), "filename": safe_name(upload.name) + "_split.zip"}
                    status.empty()
        # 此处所有上传副本、SQLite、候选和输出临时文件都已经被清理。
        saved = st.session_state.get("split_result")
        if saved:
            report = saved["manifest"]
            st.success(f"切分完成：生成 {len(report['parts'])} 个文件，输出 {report['output_count']:,} 个要素。")
            st.write(f"输入 {report['input_count']:,} = 输出 {report['output_count']:,} + 跳过 {report['skipped_count']:,}；修复 {report['repaired_count']:,}。")
            if report["skipped_count"] or any(p["oversized"] for p in report["parts"]):
                st.warning("存在跳过的几何或单要素超限，请查看日志和 manifest.json。")
            st.download_button("下载全部切分结果（ZIP）", data=saved["data"],
                               file_name=saved["filename"], mime="application/zip")
            with st.expander("输出文件列表与实际大小"):
                st.dataframe(report["parts"], use_container_width=True, hide_index=True)
            with st.expander("处理日志"):
                st.text("\n".join(report["log"]) or "无警告。")
                if report["log_truncated"]:
                    st.caption(f"另有 {report['log_truncated']} 条日志未展示；总计数完整保留。")
    except Exception as exc:
        st.session_state.pop("split_result", None)
        st.error(f"处理失败：{exc}")
        st.exception(exc)
