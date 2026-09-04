# DJI 辐射定标工具：安装包构建与维护

本安装工程只负责独立的 Windows 辐射定标程序，不包含 WebODM、Docker、WSL、DJI Thermal SDK、原始影像或定标项目。

## 1. 安装包能做什么

- 生成一个压缩的 `.msi` 文件，便于传输到其他电脑。
- 安装向导选择程序目录；默认安装到 `C:\Program Files\DJI Radiometric Calibrator`。
- 创建桌面、开始菜单快捷方式，并登记到 Windows 的“程序和功能”。
- 再次运行同一个 MSI，进入维护界面，执行修复或删除。
- 使用更高的三段版本号安装包升级，阻止旧版本覆盖新版本。
- 卸载只移除 MSI 管理的程序文件、快捷方式和安装登记；不清理用户影像、系数项目、历史路径等偏好设置。

这是所有用户安装，需要管理员授权。目标为 Windows 10 1809+ / Windows 11 的 x64 环境，仍需在实际目标电脑验收。目标电脑不需要 Python、.NET SDK、WiX、WSL 或 Docker。ARM64 不在当前验证范围内。

## 2. 构建电脑准备

只有制作 MSI 的电脑需要构建工具。有两条路线，任选其一：

- 推荐简单路线：构建命令添加 `-UseStandaloneWix`，脚本从 NuGet 下载固定版本的 WiX 官方工具到工程生成目录，使用 Windows 的 .NET Framework 4.7.2+，不要求安装 .NET SDK。
- MSBuild 工程路线：安装 [.NET 8 SDK x64](https://dotnet.microsoft.com/en-us/download/dotnet/8.0)，注意是 **SDK，不是 Runtime（运行时）**，然后不带 `-UseStandaloneWix` 构建。

安装完成后重新打开 PowerShell，检查：

```powershell
dotnet --list-sdks
```

应至少显示一行 SDK 版本，例如 `8.0.xxx`。空白表示只有运行时或没有 SDK。

安装项目固定使用 WiX Toolset 4.0.6 和同版本 UI 扩展，首次构建通过 NuGet 下载。无需手动全局安装 WiX。发布前仍应核对应用及其 Python、Qt、OpenCV、GDAL/Rasterio 等依赖的许可证与分发义务；安装提示页不代替这些许可证。

## 3. 一键生成安装包

在工程根目录执行（工程路径可以不同）：

```powershell
cd F:\DJ_image_preprocessing

powershell -ExecutionPolicy Bypass -File `
  local_tools\radiometric_calibrator\build_windows_installer.ps1 `
  -Version 2.0.0 -UseStandaloneWix
```

脚本会先构建 EXE，再检查发布目录、生成 MSI 文件清单、测试 EXE 启动，最后编译安装包。它不会执行安装。

如果刚刚已经构建了最新 EXE，可省略 EXE 重构建：

```powershell
powershell -ExecutionPolicy Bypass -File `
  local_tools\radiometric_calibrator\build_windows_installer.ps1 `
  -Version 2.0.0 -SkipExeBuild -UseStandaloneWix
```

输出位于以下目录（可能包含语言子目录，以脚本打印的完整路径为准）：

```text
local_tools\radiometric_calibrator\installer\release\
    DJI_Radiometric_Calibrator_2.0.0_x64.msi
```

只需传输这个 MSI，不需要再手动传输 `_internal`。脚本会打印 SHA256，便于传输前后比较。

如果暂不想下载构建工具，可以只验证发布目录并生成 WiX 文件清单：

```powershell
powershell -ExecutionPolicy Bypass -File `
  local_tools\radiometric_calibrator\build_windows_installer.ps1 -PrepareOnly
```

`-PrepareOnly` **不生成 MSI**，不代表安装包已经编译通过。它要求已存在 EXE 发布目录和隔离 Python 环境。

## 4. 在新电脑安装、修复、卸载

### 安装

双击 MSI，按向导选择目录并完成安装。安装后通过桌面快捷方式启动。把影像和定标项目保存在自己的数据目录，不要保存在 Program Files 程序目录。

### 修复

关闭程序，重新运行当初安装的同版本 MSI，选择“修复”。修复针对缺失或损坏的程序文件，不针对影像、ROI、系数正确性。

需要强制重新复制所有程序文件时，在管理员 PowerShell 中执行（将路径改为实际 MSI 路径）：

```powershell
$MSI = 'D:\Installers\DJI_Radiometric_Calibrator_2.0.0_x64.msi'
msiexec.exe /fa "$MSI" /norestart /L*v "$env:TEMP\dji-calibrator-repair.log"
```

请保留原始 MSI；Windows 修复过程中可能要求原安装源。

### 卸载

关闭程序，在 Windows“已安装的应用”中找到“DJI 多光谱辐射定标工具”并卸载；也可重新运行 MSI 选择删除，或执行：

```powershell
$MSI = 'D:\Installers\DJI_Radiometric_Calibrator_2.0.0_x64.msi'
msiexec.exe /x "$MSI" /norestart /L*v "$env:TEMP\dji-calibrator-uninstall.log"
```

卸载不会操作 Docker 卷，也不会递归删除用户目录。当前程序的 QSettings 偏好设置仍保留，方便重装后继续使用。

## 5. 升级规则

每次发布代码或依赖更新，都提升 MSI 三段版本号，例如 `1.0.0 → 2.0.0`，重新构建后分发新 MSI。不要用同一版本号发布内容不同的包，也不要使用第四段版本号区分更新。

`Package.wxs` 中的 `UpgradeCode` 必须保持不变；文件组件的 GUID 按相对安装路径稳定生成。应用升级需要关闭正在运行的 EXE，安装器可能提示关闭或重启。

安装工程不会自动迁移项目 JSON 内的绝对影像路径；更换数据盘符后仍需处理项目中的旧路径。

## 6. 发布前验收（推荐一次性 Windows 虚拟机）

1. 首次安装，确认可选目录、桌面/开始菜单快捷方式和应用登记。
2. 启动程序、打开一批测试影像、关闭程序。
3. 只在测试机删除一个已安装的依赖文件，再执行修复，确认文件恢复且程序能启动。
4. 用更高版本 MSI 升级，确认只保留一个应用登记，再尝试降级，确认被阻止。
5. 在程序安装目录之外创建一个测试项目，卸载程序，确认快捷方式和已安装程序文件被删除，但测试项目、影像和系数保留。
6. 在没有开发环境的第二台机器上再次验证 Qt、Rasterio 和中文路径。

编译通过不等于完成以上安装生命周期测试。工程不含自动安装/卸载脚本，避免误操作工作电脑。当前 MSI 没有数字签名，Windows 可能显示未知发布者；正式广泛分发前应使用自己的代码签名证书签名，不应让用户关闭系统安全防护。

`1.0.0` 已通过轻量 WiX 路线编译及文件表检查。`2.0.0` 增加任务清空、交互优化、原目录保存与依赖许可证元数据，独立工具的 18 项核心/GUI/安装工程测试通过。源码测试、启动自检和 MSI 数据库只读检查不等同于干净 Windows 测试机上的安装/修复/卸载全流程验收，后者仍需执行。

可单独重复只读检查（不会安装）：

```powershell
powershell -ExecutionPolicy Bypass -File `
  local_tools\radiometric_calibrator\installer\verify_msi.ps1 `
  -MsiPath local_tools\radiometric_calibrator\installer\release\DJI_Radiometric_Calibrator_2.0.0_x64.msi
```

## 7. 工程文件

| 文件 | 用途 |
| --- | --- |
| `build_windows_installer.ps1` | 构建入口、版本及工具检查、EXE 自检 |
| `installer/RadiometricCalibrator.wixproj` | WiX/MSBuild 项目及固定工具版本 |
| `installer/Package.wxs` | 安装目录、快捷方式、维护向导、升级标识 |
| `installer/generate_payload.py` | 只从 EXE 发布目录生成稳定的文件组件 |
| `installer/verify_msi.ps1` | 只读检查已编译 MSI 的文件、维护界面与升级规则 |
| `installer/Notice.rtf` | 安装时显示的数据保留与第三方组件说明 |

`generated`、`obj`、`bin` 和 `release` 是生成物，不提交 Git。安装维护界面采用 WiX 的标准 [WixUI 对话框库](https://docs.firegiant.com/wix/tools/wixext/wixui/)，不是手写删除文件的卸载程序。
