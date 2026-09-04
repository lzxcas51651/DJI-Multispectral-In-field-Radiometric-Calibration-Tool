# DJI Multispectral In-field Radiometric Calibration Tool

DJI P4M / M3M 多光谱现场辐射定标工具，独立 Windows 桌面程序。当前版本 **2.1.0**。

### v2.1.0 后台处理与进度

最新源码补丁：P4M 可输入 5 个光谱波段加末尾 Alpha，M3M 可输入 4 个光谱波段加末尾 Alpha。Alpha 通过 TIFF 颜色解释或包含 Alpha 的波段描述识别，不需要系数，也不作为输出光谱波段。未识别的额外波段仍禁止继续。右侧系数列表（计算后及载入后）均按 Blue、Green、Red、RedEdge、NIR 显示，M3M 不含 Blue；这仅改变显示顺序，不改变系数值。

v2.1.0 固定波段约定：P4M 固定对应输入/输出 1～5 为 Blue、Green、Red、RedEdge、NIR；M3M 对应 1～4 为 Green、Red、RedEdge、NIR。映射窗口仅确认该约定，不交换波段；输入数量或系数不完整时禁止继续。该约定要求输入确实来自此固定顺序的拼接流程，文件描述显示供核对。

输出保留输入 DN 正射 TIFF 的空间参考及可复制的文件级/波段级标签、自定义元数据域。原始标签、描述、单位、缩放系数等另存到 TIFF 内的 SOURCE_METADATA/original_json。DN 统计值、NoData、缩放和单位不能直接作为反射率属性沿用：输出采用 Float32/NaN、单位反射率、缩放 1/偏移 0，原值留作来源记录。压缩、分块等结构信息由输出重新生成，XML 元数据存入来源记录；外部 sidecar 文件不会自动复制。

输出反射率 TIFF 按对应输入波段的编号排序，与输入的相对波段顺序一致，不受系数 JSON 顺序影响；各输出波段记录 `source_band_index`。仅输出已配置定标系数的波段，不混入未经定标的 DN 波段。若需保持波段数量和编号完全一致，必须为所有输入波段配置系数和映射。

计算并保存系数、应用到 DN 正射影像均在后台线程运行，显示百分比及当前处理步骤，界面不再同步等待整个计算。运行期间暂时禁用编辑和重复提交，结束后恢复。系数进度按 ROI/波段统计，影像转换按处理块统计，并非剩余时间预测；末尾压缩写入期间会暂留 99%。

DN 正射波段映射改为同一个窗口：每个定标波段选择对应输入编号，全部选择后一次确认。漏选或重复选择会禁用确认并提示；波段描述不明确时不猜测顺序。可在同一窗口设置是否裁剪到 0～1。成果先写入临时 TIFF，成功后替换目标文件，失败保留原有成果；需为临时成果预留磁盘空间。程序拒绝覆盖输入 DN 文件。

### v2.0 定标布预设

每次绘制 ROI 后，可选择 Panel-01～Panel-10 或输入自定义编号。默认勾选“所有波段使用同一反射率”，例如 50% 填 `0.5`；取消勾选即可逐波段填写证书反射率。点击确定后，保存该编号的预设及最近选择，下次打开自动恢复；取消不保存。预设按传感器波段组合分别记忆，P4M 与 M3M 不混用。修改预设只影响新建 ROI，不会改写历史 ROI。

预设保存在当前 Windows 用户的 `HKEY_CURRENT_USER\Software\DJI-Local-Tools\Radiometric-Calibrator`，不在影像文件夹内。清空当前任务不会清除预设；换电脑不会自动迁移。每批次使用前应核对定标布证书，不能因为记忆功能而忽略实际反射率差异。

在 RGB 照片上勾选定标布，填写各波段的已知反射率，程序在计算时自动关联多光谱影像、配准 ROI、统计 DN 并拟合定标系数。也可将系数应用到 DN 多波段正射影像，输出 Float32 反射率 GeoTIFF。

本工具是独立项目，非 DJI 官方软件；不内置 WebODM，不负责影像拼接，不是热红外温度转换工具。

## 下载安装

进入 [Releases](https://github.com/lzxcas51651/DJI-Multispectral-In-field-Radiometric-Calibration-Tool/releases)，下载 `DJI_Radiometric_Calibrator_2.1.0_x64.msi`。只需传输这个安装文件，不需要另复制运行库。若仓库为私有，需使用有权限的 GitHub 账号登录。

- 目标环境：Windows 10 1809+ / Windows 11，x64；其他系统未验证。
- 运行软件无需安装 Python、.NET SDK、WSL、Docker 或 DJI Thermal SDK。
- 安装需要管理员权限，支持选择目录、桌面/开始菜单快捷方式。
- 已安装 1.0.0 的用户：关闭旧程序，运行 2.1.0 安装包升级。
- 修复：重新运行同版本 MSI，选择修复；卸载：Windows“已安装的应用”，或 MSI 维护界面。
- 卸载不删除原始影像、定标项目或用户偏好设置。
- 安装包暂未数字签名，可能显示未知发布者。核对来源和 Release 的 SHA256，不要关闭系统安全保护。

安装与维护细节见 [安装包构建与维护](DJI_辐射定标工具_安装包构建与维护.md)。

## 功能与操作

| 功能 | 行为 |
| --- | --- |
| 传感器识别 | 读取影像 EXIF/XMP 和标准文件名，支持 P4M、M3M；无需航线文件 |
| 照片浏览 | 左侧只显示 RGB；文件名位于缩略图下方，原始窄波段仍保留参与计算 |
| 自动查找 | 默认关闭，点击后才快速扫描最多 160 张 RGB，返回候选供人工确认 |
| 手动导入 | 可导入 RGB 定标布照片；计算前需关联齐全的同次拍摄多光谱波段 |
| ROI 标注 | RGB 矩形/多边形 ROI，多图多区域，编号与列表联动，可禁用或删除 |
| 画布交互 | 中键拖动、以鼠标为中心滚轮缩放；无独立平移按钮 |
| 窗口布局 | 三栏宽度可拖动，保存窗口大小、栏宽及最近打开路径 |
| 清空任务 | 无需关闭程序；清空界面和未保存标注，不删除磁盘文件 |
| 配准与定标 | 点击计算时才执行 RGB→各波段配准、DN 统计、逐波段拟合 |
| 保存系数 | 直接写入原始批次目录，固定英文文件名；已有文件先确认覆盖 |
| 正射转换 | 逐块应用系数，输出 Float32 反射率 GeoTIFF，手动确认波段顺序 |

## 新手开始

1. 准备一个批次文件夹，保留同次拍摄的 RGB 和多光谱原片；不要只复制 RGB。
   - P4M：RGB + Blue、Green、Red、RedEdge、NIR。
   - M3M：RGB + Green、Red、RedEdge、NIR。
2. 打开软件，点击“打开批次”，选择该文件夹。没有航线文件不影响读取影像元数据。
3. 左侧选择拍到定标布的 RGB；也可以点击“自动查找定标布”辅助筛选。自动结果不是最终识别结论。
4. 中键拖动、滚轮放大，在工作区上方选择矩形或多边形。只圈定标布内部，避开边框、阴影、反光及背景。多边形单击加顶点，双击结束。
5. 填写定标布编号和证书中的各波段反射率，单位为比例（例如 0.5，而不是 50）。可在多张照片上继续添加 ROI。
6. 点击“计算并保存系数”。无需选择目录，也不会创建 `_calibration` 文件夹。已有系数文件时会询问是否覆盖。
7. 检查模型和配准质量提示；低质量或几何缩放后备结果必须复核。
8. 点击“应用到 DN 正射影像”，选择未做现场反射率定标的多波段 GeoTIFF，确认各波段映射、输出文件位置及是否裁剪到 0～1。
9. 处理下一批时点击“清空当前任务”，确认后再次打开批次；要重做同一批，也按此操作。未保存的标注会丢失，已保存文件不会删除。

典型数据目录：

```text
flight_batch/
  原始RGB照片.JPG
  同次拍摄的多光谱波段.TIF
  radiometric_calibration_coefficients.json
```

JSON 保存原图路径、RGB ROI、各波段已知反射率、映射后 ROI、DN 统计、配准方法/分数和模型。再次“载入系数项目”可恢复保存的标注。JSON 中影像路径目前为绝对路径，迁移数据目录后需相应更新路径。

## 算法与质量边界

- 配准：OpenCV ORB 特征 + RANSAC 单应性；不足时尝试 ECC 仿射；仍失败时采用归一化坐标后备并告警。同一照片的多个 ROI 复用当次计算中的配准结果，不在浏览时预计算。
- DN 统计：保留原始单波段值，输出均值、中位数、2% 截尾均值、标准差和 CV；大 ROI 适度收缩边缘，减少混合像元。
- 同一反射率水平：`ρ = a × DN`；两个及以上不同水平：`ρ = a × DN + b`，支持稳健拟合。重复拍同一个灰度水平不能代替多个已知反射率水平。
- 性能：JPEG 缩放解码、最多三张工作区预览缓存；交互时快速插值，结束后恢复平滑显示。缩略图只用于显示，系数仍用原始窄波段数据计算。

**自动配准成功不代表 ROI 一定准确。** 多镜头视差、低纹理、NIR 外观差异、近距离拍板、饱和或过小的 ROI 都可能造成误差。几何缩放后备仅是估计，不等于可靠配准；不得未经检查直接用于正式定量成果。

当前工作于原始 DN 域，尚未实现逐幅曝光、增益、黑电平、暗角归一化。现场定标照片与飞行照片应具有兼容的曝光/增益和光照条件；变化明显时，不能盲目将一个系数套用整批影像。对 WebODM 输出应用模型前，还需确认未经过会改变 DN 尺度的色彩均衡或辐射处理，并使用无损输出。

完整说明：[开发与使用手册](DJI_多光谱现场辐射定标工具_开发与使用说明.md)。

## 源码运行与开发

```powershell
git clone https://github.com/lzxcas51651/DJI-Multispectral-In-field-Radiometric-Calibration-Tool.git D:\Code\DJI-Radiometric
cd D:\Code\DJI-Radiometric
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r local_tools\radiometric_calibrator\requirements-windows.txt
.\.venv\Scripts\python.exe local_tools\dji_radiometric_calibrator.py
```

测试（包括无窗口 GUI 回归测试）：

```powershell
cd D:\Code\DJI-Radiometric\local_tools
..\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_radiometric*.py' -v
```

从仓库根目录构建 MSI：

```powershell
powershell -ExecutionPolicy Bypass -File `
  local_tools\radiometric_calibrator\build_windows_installer.ps1 `
  -Version 2.1.0 -UseStandaloneWix
```

该命令构建隔离 EXE、下载固定 WiX 工具、生成文件清单、做启动自检并编译/检查 MSI；不会自动安装软件。输出位于 `local_tools/radiometric_calibrator/installer/release/`。已有最新 EXE 时可加 `-SkipExeBuild`。发布新版本需递增三段版本号，保留稳定的 UpgradeCode。工具、依赖、生成目录与安装包不进入 Git；安装包上传 Release。

## 工程结构

```text
local_tools/
  dji_radiometric_calibrator.py     入口
  radiometric_calibrator/
    gui.py                        界面、任务清空、画布交互
    catalog.py / metadata.py      机型、波段和曝光组识别
    candidate.py                  按需快速候选查找
    registration.py / roi.py      配准、区域映射与统计
    calibration.py / project.py   模型及系数JSON
    geotiff.py                    分块正射转换
    build_windows_*.ps1           EXE/MSI构建
    installer/                    WiX安装维护工程
  tests/                          核心、GUI与安装工程测试
```

## 数据安全与分发说明

- 不自动上传影像；图像处理在本机执行。构建阶段需要联网下载 Python/WiX 依赖。
- 清空任务不删除磁盘文件；卸载不清理影像、项目和偏好设置。
- 不将数据存入程序安装目录。原始影像目录必须可写，才可按默认位置保存系数。
- MSI 尚未数字签名，也未完成干净 Windows 机器上的完整安装/修复/卸载验收；发布前应按维护文档检查。
- 第三方依赖保持其原有许可，发布包包含相应元数据/许可证文件。本仓库尚未为自有代码指定统一开源许可证；代码可见性不应被理解为任意再分发授权。

版本变化见 [CHANGELOG](CHANGELOG.md)。
