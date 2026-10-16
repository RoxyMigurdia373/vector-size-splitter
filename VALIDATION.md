# 本地验收记录

日期：2026-10-16。环境：Windows、Python 3.12，独立虚拟环境安装本项目锁定依赖。

- `python -m pytest -q`：35 个测试通过，包含 Streamlit 上传→切分→ZIP 内容校验。
- `python -m pip check`：无依赖冲突。
- `python tests/benchmark_100mb.py`：输入 104,863,011 字节、12,640 个要素，10MiB 阈值输出 11 个 GeoJSON，每个均不超限；全部要素顺序及内容 SHA256 相同。本机切分与回读校验约 2.23 秒（不含输入生成），不代表云端性能。
- Shapefile/GPKG 的实际大小、属性回读、再输入切分均由测试覆盖。
- Streamlit Community Cloud 已发布：https://vector-size-splitter-roxy.streamlit.app/ ，运行 Python 3.12.15。
- 云端浏览器完整流程通过：上传 examples/example.geojson，阈值 0.3KB（307 字节），下载 ZIP 含 3 个各 185 字节的 GeoJSON；3 个要素完整内容和属性与输入一致，跳过数为 0。
- Linux Docker 镜像构建、真实第三方 File Geodatabase 数据和云端大文件压力测试尚未实测。

未测量峰值内存，不承诺 500MB 文件在免费 Cloud 的内存限制下成功。大文件建议 Docker 部署。
