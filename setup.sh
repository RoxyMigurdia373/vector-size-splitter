#!/usr/bin/env bash
# 可选：仅确实需要 osgeo 时手动执行；本应用使用 Fiona，不需要它。
set -euo pipefail
python -m pip install "GDAL==$(gdal-config --version)"
