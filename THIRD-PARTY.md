# 依赖及第三方许可清单

核对日期：2026-10-22。此文件不改变任何第三方的许可证。

## 运行依赖

- Python 标准库：目标运行环境 Python 3.12+。解释器不随此源码 ZIP 分发，继续适用 Python 自身许可。
- Pillow 13.0.0：唯一直接 Python 运行依赖，requirements.txt 固定版本。上游声明 MIT-CMU，要求保留相关版权、许可及免责信息。[PyPI 固定版本元数据](https://pypi.org/project/pillow/13.0.0/)
- 浏览器前端：原创 HTML / CSS / JavaScript，没有外部脚本、字体、图标包或前端框架依赖。

本源码包不携带 Pillow 源码、wheel、原生库或 Python 解释器。安装后不要删除 Pillow 的 dist-info/licenses/LICENSE。某些平台 wheel 捆绑其他原生库，并在该文件中一并提供 notice；以实际安装平台和 wheel 为准，不要拿另一平台的清单替代。

可选测试工具 Node、Playwright 和 Chromium 只用于验证，不是运行依赖，也未打入 ZIP。Dockerfile 使用官方 Python 基础镜像；镜像和其中操作系统包需各自保留许可与 notice，正式发布镜像时另做最终镜像清单。此处不是完整容器 SBOM。

## 为什么从 12.3.0 改到 13.0.0

官方 13.0.0 版本于 2026-10-16 发布，安全公告于 2026-10-17 发布，明确列出旧版本影响范围及 13.0.0 修复版本：

- [GHSA-v9pv-5gc8-w2m6 / CVE-2026-96639](https://github.com/python-pillow/Pillow/security/advisories/GHSA-v9pv-5gc8-w2m6)：多帧加载中的原生存储复用问题，影响 11.0.0 至 12.3.0。
- [GHSA-5x8w-8jj5-q52h](https://github.com/python-pillow/Pillow/security/advisories/GHSA-5x8w-8jj5-q52h)：特定 FPX/JPEG 解码路径越界，影响 12.3.0 及之前版本。
- [GHSA-8g5g-8vmr-9782](https://github.com/python-pillow/Pillow/security/advisories/GHSA-8g5g-8vmr-9782)：特定 JPEG draft / 缩放路径越界，影响 12.3.0 及之前版本。

官方完整修复清单：[Pillow 13.0.0 release notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html)。

这些公告并不证明本应用的受限路径都可触发：本应用不使用多帧遍历或 JPEG draft，不配置 FPX 的可选依赖，且现在在打开图片时只允许 PNG / JPEG / WebP 解析器。仍不能以此代替升级，首包固定的旧依赖不应继续用于部署。没有运行漏洞样本或对任何外部服务做攻击验证。

已在隔离目录安装官方 PyPI 的 13.0.0 CPython 3.12 Linux x86_64 wheel，确认运行版本后完成回归；详细结果见 TEST-REPORT.md。

实际测试 wheel 的许可证文件包含 Pillow 原始许可及 AOM、Brotli、bzip2、dav1d、FreeType、HarfBuzz、Little CMS、libavif、libjpeg、liblzma、libpng、libtiff、libwebp、libyuv、OpenJPEG、libraqm、Tcl/Tk、libXau、libxcb、libXdmcp、zlib 和 zstd 的 notice。这个清单仅说明已检查的 wheel，不替代各组件许可证全文，也不保证其他平台 wheel 相同。
