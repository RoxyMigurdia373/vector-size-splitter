# 按文件大小切分矢量数据

在线应用：[vector-size-splitter-roxy.streamlit.app](https://vector-size-splitter-roxy.streamlit.app/)

可部署在 Streamlit Community Cloud 或 Docker 的单文件上传应用。入口为 `app.py`，支持 GeoJSON、Shapefile ZIP、GeoPackage，以及 Fiona/GDAL 环境可读的 File Geodatabase ZIP。每次选择一个数据集中的一个图层。

## 快速运行

推荐 Python **3.12**（代码支持 3.10+；使用锁定的 wheel 依赖）。在本项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Linux/macOS：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

浏览器访问 http://localhost:8501 。上传文件 → 选择数据集/图层 → 设置 `10 MB` → 选择输出格式 → 点击“开始切分” → 下载 `原文件名_split.zip`。

可上传附带的 `examples/example.geojson`，设置 `0.3 KB` 快速观察多分片输出。

默认不新增字段、不转换坐标系。启用辅助字段后，`orig_fid` 保存驱动提供的原 feature ID（没有则采用从零开始的序号），`part_id` 是从 1 开始的**分片编号，不是每个要素的唯一 ID**。与已有字段冲突时明确报错。

GeoJSON 输出新增“几何类型”选项（文件扩展名仍为 `.geojson`）：

- **保持原类型**（默认）：不强制转换，兼容点、线、面等原有数据。
- **Polygon**：Polygon 保持不变；仅包含一个面的 MultiPolygon 转为 Polygon；包含多个面的 MultiPolygon 明确报错，**不拆分、不删除部件，也不输出部分结果**。
- **MultiPolygon**：Polygon 包装为 MultiPolygon，已有 MultiPolygon 保持不变。不同要素不会合并。

两种转换均保持原属性、ID、坐标精度、内洞和 Z 坐标，不改变要素数。指定面类型时，点、线及 GeometryCollection 明确报错；无效几何先按现有规则修复，再检查目标类型。转换后的实际字节数仍受大小阈值约束。

## 项目文件

```text
app.py                     Streamlit 界面、会话下载、临时目录生命周期
splitter.py                磁盘暂存、几何检查、精确分组、计数核对
io_utils.py                安全解压、流式读取、写入回读校验、大小与 ZIP
.streamlit/config.toml     500MB 上传配置
requirements.txt           锁定应用依赖
requirements-dev.txt       测试依赖
packages.txt               Cloud 系统依赖
Dockerfile                 Python 3.12 / Debian Bookworm
setup.sh                   可选的 osgeo/GDAL 绑定安装脚本
tests/test_splitter.py     自动化测试
tests/benchmark_100mb.py    生成 100MiB 数据并核验每个要素
```

## 大小与完整性保证

- KB、MB、GB 分别按 1024、1024²、1024³ 字节换算，小数阈值向下取整到字节。
- GeoJSON 大小包含 UTF-8 文件头、CRS、逗号、全部 feature 和文件尾，按字节精确分组。
- Shapefile 阈值是 `.shp/.shx/.dbf/.prj/.cpg` 等**全部组成文件的未压缩总和**。每个分片在总下载包内另存为 `原名_part001.zip`，其中所有组成文件同名。
- GeoPackage 阈值是关闭驱动后的 `.gpkg` 完整体积。SQLite 页、系统表等格式开销都计入。小阈值可能连空容器开销都放不下。
- 原生格式先写入独立候选目录，关闭、测量、逐属性回读验证，再通过倍增/二分选取可容纳的前缀。最终文件再次测量；绝不会将多个要素的超限分片交付。数据库页分配可能让分片不能恰好填满。
- 唯一允许超限的情况：**单个要素连同容器开销超过阈值**，独立输出，`oversized=true` 并记录日志。如果业务要求绝对不允许超限，应先提高阈值或剔除这种数据；本应用依照需求保留该要素。
- 空几何跳过；无效几何先 `make_valid`，失败后跳过。不会切割单个要素；修复可能将 Polygon 变为 MultiPolygon/GeometryCollection，属性保持不变。目标驱动不支持修复后几何类型时导出报错，请改用 GeoJSON。
- 有效数据满足 `输出数 = 输入数`；有跳过时满足 `输入数 = 输出数 + 跳过数`。两个条件不能同时无条件成立。
- `manifest.json` 记录实际大小、要素数、超限标识、修复/跳过计数和前 1000 条日志，另记录被截断日志数。无属性变更操作。

## 属性、类型和格式边界

**任何格式之间无条件保持全部字段类型在 GIS 格式规范上不可实现。** 本应用采用严格模式：能无损表达则输出；发现字段名、类型或值发生变化则报错，整个任务不提供部分下载结果。

| 场景 | 行为 |
| --- | --- |
| GeoJSON → GeoJSON | 直接流式解析原始 properties，保留嵌套对象、数组、布尔值、null、大整数和高精度十进制；不通过 DataFrame 自动类型推断 |
| GeoJSON → SHP/GPKG | 从原始 properties 推断固定结构；混合类型、嵌套属性、字段集合不同或 null properties 时拒绝 |
| SHP/GPKG → 原生格式 | 传递 Fiona schema，关闭文件后检查字段名、类型族与每个属性值；不兼容时拒绝 |
| SHP 长字段名 | 超过 10 个 UTF-8 字节时拒绝，不自动映射或截断；选 GeoJSON/GPKG |
| SHP 文本/数字损失 | DBF 文本截断、空字符串变 null、精度损失等由回读校验发现后拒绝 |
| date/datetime → GeoJSON | JSON 没有原生日期类型，严格模式拒绝；选择 GPKG |
| 内部 FID | GPKG 使用不与属性冲突的内部主键列；已有普通 `fid` 属性仍原样保存 |
| GeometryCollection/混合几何 | GeoJSON 支持；GPKG 取决于图层 schema；Shapefile 不能通用表示混合几何 |

GeoJSON 没有独立的字段 schema，也不能表示“int32 与 int64 字段宽度”这类数据库声明。原生格式回读将 `int/int32/int64` 视为整数类型族，保留实际整数值；DBF 宽度与数据库内部 SQL 类型编码不能承诺跨格式逐字相同。若必须保持数据库字段宽度、约束、域、关系、附件等，应使用专门的数据库迁移工具。本应用处理要素属性与几何，不复制 GDB 域/关系/附件、GPKG 样式和其他非要素表。

GeoJSON 顶层 feature `id` 默认在 GeoJSON 输出中保留；原生格式的驱动 feature ID 不是普通属性，写出时可重新编号。需要保留它时打开 `orig_fid`，原本位于 `properties` 内的唯一 ID 始终保持。集合级 `bbox` 等元数据不复制；坐标转换/几何修复后移除失效的 feature bbox。

源 CRS 默认保留。非 WGS84 GeoJSON 会使用传统 `crs` 成员，属于兼容旧格式的 GeoJSON，而非严格 RFC 7946；需要标准 WGS84 输出时勾选转换选项。没有 CRS 的原生输入不能直接转换。未声明 CRS 的 GeoJSON 按规范视为 WGS84。

## 内存、性能与临时文件

没有将全部要素读入 GeoDataFrame：GeoJSON 使用 `ijson` 和 Decimal 流式读写，其他格式使用 Fiona 迭代器，统一 SQLite 落盘，SQLite 页缓存限制约 4MB。GeoPandas/pyogrio 保留在依赖中供扩展；选择 Fiona 是为了流式迭代和显式 schema 控制。

工作内存仍至少要容纳一个完整要素；特别大的单要素无法保证适合免费内存环境。Shapely 几何验证也需要相应内存。原生格式反复写入与回读的精确算法比粗略估算慢，特别是小阈值产生数千个 GPKG 时。推荐 GeoJSON 输出以获取最高吞吐。

Streamlit 的上传和下载自身使用内存，不能仅依靠磁盘算法承诺 500MB 上传在约 1GB 免费层成功。默认下载 ZIP 超过 **200MB** 时明确报错保护内存；Docker 可设置环境变量 `MAX_DOWNLOAD_MB=1000` 并增加内存。压缩包条目最多 20000 个、解压后最多 4GiB，拒绝路径穿越和符号链接。单次任务应预留输入、解压、暂存、候选、输出、ZIP 所需磁盘空间，建议至少原始数据数倍的空闲容量。

`TemporaryDirectory()` 覆盖上传副本、解压目录、SQLite 和输出文件，成功/异常都自动清理。只将最终 ZIP 字节和轻量清单保存在 session_state，便于界面重运行下载；更换参数或上传文件时清除旧结果。会话下载及并发用户的内存总量仍需要部署层管理。异常任务不发布部分成功结果。

## Streamlit Community Cloud 部署

1. 将**本目录内容作为 GitHub 仓库根目录**推送，包含隐藏的 `.streamlit/config.toml`。
2. 在 Streamlit Community Cloud 创建应用，选择仓库和分支，入口 `app.py`。
3. 高级设置选择 **Python 3.12**。平台读取 `requirements.txt` 和 `packages.txt`。
4. 部署后用小 GeoJSON、Shapefile ZIP 和 GPKG 验证上传/切分/下载。
5. 大数据或多人并发使用 Docker，500MB 上传上限不代表免费层具备处理该体积的资源。

Fiona/Pyogrio wheel 通常自带 GDAL，系统 `gdal-bin` 版本与 wheel 的版本可能不同，这是预期情况。本应用不 import `osgeo`，**不需要执行 setup.sh**。只有额外功能确实需要系统 GDAL Python 绑定时才手动运行该脚本；它安装完整 `gdal-config --version` 对应版本，可能需要编译器和开发头文件。Cloud 不保证自动执行任意 setup.sh。

GDB 读取是否可用由当前 Fiona/GDAL 驱动决定，可检查：

```bash
python -c "import fiona; print(fiona.__gdal_version__); print(fiona.supported_drivers)"
```

`OpenFileGDB`/`FileGDB` 可读取时支持上传 `.gdb` 目录 ZIP。没有驱动时界面显示真实错误；请转为 GPKG，或使用带驱动的 Docker 环境。加密/专有扩展、无权限或损坏 GDB 不保证读取。

`.streamlit/config.toml` 默认保持 CORS/XSRF 保护。遇到上传 403 先检查反向代理、Host 和 HTTPS 配置；只有受控环境明确需要时才同时改为 `enableXsrfProtection=false`、`enableCORS=false`。

## Docker 部署

```bash
docker build -t vector-size-splitter .
docker run --rm -p 8501:8501 --memory=2g -e MAX_DOWNLOAD_MB=500 vector-size-splitter
```

容器以非 root 用户运行并提供健康检查。部署平台须允许足够的临时磁盘；若使用 tmpfs 存放 `/tmp`，临时数据会计入容器内存，建议使用磁盘挂载。反向代理需支持 WebSocket 和足够大的请求体。

## 测试与 100MB 验收

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python tests/benchmark_100mb.py
```

基准脚本自动生成至少 100MiB 的 GeoJSON，设置 10MiB 阈值，检查每个文件实际大小、总数，以及全部要素按顺序序列化的 SHA256 一致，最后自动删除临时数据。测试包含三种输出、原生输入、中文/大整数/高精度属性、空/无效几何、不同几何类型、辅助字段、CRS、ZIP 安全检查和 Streamlit 启动。

已于 2026-10-16 部署到 Streamlit Community Cloud（Python 3.12），完成示例数据上传、切分、下载及属性回读校验。Docker 镜像构建与云端大文件压力测试尚未执行。

## 直接调用核心逻辑

```python
import tempfile
from pathlib import Path
from splitter import split_vector
from io_utils import pack_outputs

with tempfile.TemporaryDirectory() as tmp:
    result = split_vector(
        "examples/example.geojson", Path(tmp) / "parts", 10 * 1024**2,
        output_format="geojson", add_fields=False, wgs84=False,
    )
    manifest = {**result, "parts": [
        {k: v for k, v in part.items() if k != "path"}
        for part in result["parts"]
    ]}
    pack_outputs(result["parts"], "example_split.zip", manifest)
```
