"""版本日志解析器。

从 VERSION_LOG.md 解析为结构化数据，供 GUI 和 CLI 使用。
支持标准 Markdown 格式（## 标题 + 粗体日期/作者 + ### 修改内容 + 列表项）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Embedded release history; refreshed from VERSION_LOG.md by build.py.
EMBEDDED_VERSION_LOG = '# 版本日志\n\n> 本文件记录软件的所有历史版本、修改内容和作者。\n> 按时间倒序排列（最新版本在最上面）。\n> 编辑规范：新增版本时复制最后一个版本块，修改版本号、日期、作者和修改项即可。\n\n---\n\n## v1.0.27\n\n**日期**：2026-11-26\n**作者**：zhw\n\n### 修改内容\n\n- **采纳预测图标**：改为醒目的绿色实心圆勾选图标，与删除按钮使用相同尺寸和交互，悬停显示操作说明。\n- **类别索引**：人工和预测框左上角显示紧凑类别 ID，固定屏幕字号，重叠时横向错开；保留来源色块和红色待核对提示，不显示长类别名称。\n- **顶部布局**：图片信息改为靠左排列；右侧检查栏起点左移约 60 像素，避免页签挤在窗口右边。\n\n---\n\n## v1.0.26\n\n**日期**：2026-11-26\n**作者**：zhw\n\n### 修改内容\n\n- **拖动条调度**：拖动仅更新目标序号；停留 180ms 加载小预览，松手才正式切图、保存上一张修改并加载清晰原图；程序同步滑块位置不会反向触发切图。\n- **附近小缓存**：默认仅保留当前一张原图与前后各一张 960×600 内的预览；空闲 250ms 后才预加载，跳远淘汰旧预览，不缓存相邻完整原图。\n- **任务优先级与取消**：当前图解码独立于可选预览；每个通道只保留一个正在执行任务及一个可替换的最新任务，开始解码前与完成后均检查是否过期，旧结果不会覆盖新目标。\n- **绘制与核对**：复用画布框线与来源色块，减少平移时创建 / 删除控件；较大的人工 / 预测匹配使用独立后台任务并忽略过期结果。\n- **恢复草稿**：编辑停止后定期将未保存标注写入独立草稿，重新访问图片时询问恢复；恢复不直接覆盖标签，正常保存后移除对应草稿，草稿写入错误可见。\n- **按需操作**：采纳预测、确认无误、取消扫描 / 导入按钮仅在适用状态显示；复用未变更的标签索引，减少重复扫描。\n- **结构整理**：将最近任务调度、图片加载管线和恢复草稿分成独立模块，退出时取消任务、释放预览并停止专用工作线程。\n\n---\n\n## v1.0.25\n\n**日期**：2026-11-26\n**作者**：zhw\n\n### 修改内容\n\n- **类别选择**：移除常驻绘制类别，新建 HBB / OBB / 多边形后弹出可搜索类别下拉框，记住上次选择，取消不生成标注。\n- **直观核对**：隐藏画布标签文字；人工实线、预测虚线，来源小色块防止重叠遮挡；仅单方存在、类别不一致或 IoU 低于 0.65 的对应目标统一红色，由用户判断，不增加异常分类。\n- **编辑效率**：支持重叠对象切换、人工与预测统一对象列表、选中详情、采纳预测、会话内确认无误和最多 30 步撤销；数据或标注变化后重新核对。\n- **统一导入**：单一导入标签入口，选择人工 / 预测、目录 / JSON、当前或历史类别表，预检 YOLO 类别 ID 并支持取消；类别默认全部显示、递归目录默认开启。\n- **大图性能**：后台仅渲染可见区域，合并连续缩放与平移请求，停止后补清晰图，Tk 图像仅由主线程创建；相邻预加载限制为两张范围，图像缓存最多 6 张且受 256 MiB 预算约束（单张超大图除外）。\n- **生命周期**：丢弃过期渲染 / 扫描结果，错误可见并保留未保存编辑，增加响应监测，退出取消任务并释放缓存与渲染线程池。\n- **界面**：左侧图片库、中间画布、右侧标注检查；固定简洁来源图例，底部显示缩放与保存状态，提供取消扫描 / 导入入口。\n- **内置日志**：程序直接读取内置版本历史，打包同步 Markdown，运行时无需外部 VERSION_LOG.md。\n\n---\n\n## v1.0.24\n\n**日期**：2026-11-24\n**作者**：zhw\n\n### 修改内容\n\n- **布局重构**：左侧工具栏迁移至顶部水平工具栏（浏览 / 编辑 / 三种绘制工具 / 适应 / 显隐）；图片库与文件名搜索整合至右侧下半区，参数面板在上、图片库在下可拖动分割；图片打开入口收敛为「打开图片」按钮 + Ctrl+O + 空状态引导，去除重复按钮。\n- **标注快捷键**：对齐主流标注软件（LabelImg / X-AnyLabeling）——R/W 矩形、O 旋转框、P 多边形、E 选择、V 浏览、A/D 切图、F 适应、T 显隐、Delete 删除、Ctrl+Z 撤销、Ctrl+S 保存。\n- **类别选择对话框**：修改类别 / 添加标注类别统一为下拉选择「序号·名称」，支持输入序号自动定位、名称精确匹配、新名称直接创建；下拉点选即确认，无需再点确定。\n- **删除简化**：标注列表与画布均支持右键直达「修改类别 / 删除此标注」，右键拖动仍为平移。\n- **类别过滤芯片**：过滤嵌入「当前图片的标注」区，彩色芯片带 ✓ 对钩显示（无叉号），点击切换，自动换行。\n- **加载反馈**：切图时画布中央显示「图片加载中…」并同步状态栏提示。\n- **自动保存常开**：移除自动保存开关与切图确认弹窗，修改后切图自动落盘。\n- **入口去重**：保存 / 导入 / 导出统一走菜单与快捷键，移除工具栏与面板中的重复按钮。\n\n---\n\n## v1.0.23\n\n**日期**：2026-11-23\n**作者**：zhw\n\n### 修改内容\n\n- **统一图片工作台**：图片导入即预览；左侧目录历史、文件名搜索与分页列表，中间预览，右侧概览 / 标签检查 / 分类整理；顶部只保留图片工作台、视频抽帧、相似去重。\n- **简化分类转移**：分类按钮直接标记；选择分类、目标目录后转移；分类管理默认折叠，自动下一张改为显式选项。\n- **常用标注编辑**：参考 X-AnyLabeling 的交互，独立实现 HBB、OBB 和 polygon 的创建、选择、移动、顶点调整、OBB 旋转、类别修改、删除与撤销；提供对象列表与绘制工具选择。\n- **统一导入导出**：支持 YOLO、LabelMe / X-AnyLabeling JSON、COCO JSON；转换对话框选择范围与目标任务，自动转换坐标和类别 ID，新目录输出、相对层级与转换报告。\n- **数据完整性**：修复多点 YOLO 分割被截断、COCO bbox 与 segmentation 重复、四点 LabelMe polygon 被识别为 OBB、隐藏类别保存遗漏等问题；保存失败阻止切图，未保存修改可保存 / 放弃 / 取消。\n- **COCO mask**：支持压缩及未压缩 RLE 读取；保留未编辑的原始 RLE；含孔洞 mask 的轮廓保持只读，禁止无损条件不满足的 polygon 导出。\n- **任务与辅助界面**：视频抽帧使用完整任务页并提供查看输出入口；后台去重不抢占工作台，返回时恢复原浏览位置；日志默认收起，启动恢复改为欢迎页主动继续。\n\n---\n\n## v1.0.22\n\n**日期**：2026-11-23\n**作者**：zhw\n\n### 修改内容\n\n- **标签编辑器（X-AnyLabeling 风格）**\n  - GT detect 框支持选中/拖动/缩放/删除/新建/撤销/换类别，Tab 键在 GT/PRED 间独立切换显示\n  - gt 实线粗框 / pred 虚线细框（P: 前缀+置信度）分层显示，聚焦图片外淡化，选中框白色高亮 + 8 手柄\n  - 支持从 PRED 框导入为 GT 编辑；自动保存开关（切图/关闭前自动 flush 未保存修改）\n  - 修复编辑会话中撤销/删除/拖动后重绘从标签缓存重建、覆盖未保存编辑结果的 bug\n- **界面布局优化（按钮整合 + 参数栏三段式）**\n  - 顶部工具栏精简：移除「帮助」「关于」按钮（与菜单重复），入口统一收进「帮助」菜单\n  - 标记转移面板：新建/删除/刷新分类三按钮合并为「分类 ▾」下拉；「更改目录」降级为行内 📁 浏览按钮；「批量转移」固定底部操作区\n  - 相似去重面板：全选/全部取消/每组选一张/间隔N张选四按钮合并为「选择 ▾」；导出/导入 JSON 合并为「任务 ▾」\n  - 标签显示面板：类别文件「浏览」按钮并入「导入 / 导出 ▾」菜单\n  - 底部日志栏：历史记录/清空合并为「⋯」下拉\n  - 右侧参数栏重构为三段式：每面板内部滚动内容区 + 底部固定操作区，替代整栏共用滚动\n  - 菜单栏新增「视图」菜单：五个功能模块可从菜单直达（Ctrl+1~5），与左侧 Tab 同 command，入口统一\n- **图片导入自动发现标签**\n  - 新增标签目录发现引擎：加载图片目录后自动探测同级 labels 目录并引导类别，支持 YOLO detect/pose/obb/segment 与 COCO\n  - 数据源区合并：图片目录 / GT 标签目录 / PRED 目录统一收纳，历史路径记忆\n- **端到端测试**：新增 9 项 e2e 测试（自动发现/编辑器加载/PRED 分层/保存写回/撤销/删除新建/切图自动保存/批量导出）全部通过\n\n---\n\n## v1.0.21\n\n**日期**：2026-11-10\n**作者**：zhw\n\n### 修改内容\n\n- **相似去重新增"崩溃恢复缓存"开关**\n  - 去重面板新增勾选框，默认关闭：单机使用时完全不创建 `DedupeCache`，跳过特征缓存读写与周期落盘，扫描最快\n  - 开启后行为与旧版一致：每 200 张 / 60 秒异步落盘 `.dedupe_cache.json`，任务中断或崩溃后可续扫\n  - 开关状态持久化到配置（`dedupe_cache_enabled`），仅在开启时才弹"续扫/重扫/取消"对话框\n- **相似去重结果展示清理**\n  - 重扫/展示统一走中间画布视图（`dedupe_canvas_view`），删除旧滚动面板残留代码与死代码\n- **打包修复（build.py）**\n  - 收缩 `--exclude-module` 列表：urllib/email/asyncio、tkinter.dialog、MpoImagePlugin 等被延迟 import 依赖的模块不再排除，避免启动期 `ModuleNotFoundError`\n  - PyInstaller 子进程固定 PATH 与 `TCL_LIBRARY`/`TK_LIBRARY` 指向当前环境的 tcl8.6/tk8.6，修复 conda 多环境 tcl/tk 版本不一致导致的 `TclError: version conflict for package "Tcl"`\n  - 移除未使用的 `appdirs`（requirements.txt 与 hidden-imports 同步清理）\n\n---\n\n## v1.0.10\n\n**日期**：2026-07-01\n**作者**：zhw\n\n### 修改内容\n\n- **抽帧不再卡死资源管理器（修复"开始抽帧后文件夹打不开"）**\n  - 并发数大幅下调：`get_optimal_workers(\'io\')` 从 `min(cpu*2, 16)` 改为 `min(cpu, 4)`，避免十几个视频同时抽帧把磁盘吃满\n  - 单视频内写入线程：`write_workers` 从 `cpu/2` 改为 `min(2, cpu/2)`，避免一个视频触发 8 个 PNG 压缩 + 磁盘 IO\n  - 抽帧开始时调用 `lower_process_priority()`，把 Python 进程优先级降为 `BELOW_NORMAL_PRIORITY_CLASS`，让出 CPU/IO 给资源管理器\n  - ffmpeg 子进程同步设 `BELOW_NORMAL_PRIORITY_CLASS`\n  - ffmpeg 抽帧不再用 `dst_dir.glob()` 数文件，改用 ffmpeg `-progress pipe:1` 解析 `frame=` / `progress=end` 进度，**零磁盘扫描** → 资源管理器不再被打扰\n- **HEVC/H.265 抽帧修复**\n  - OpenCV 内置 ffmpeg 对 HEVC 支持不完整（`hvc1`/`hev1`），直接 `cv2.VideoCapture` 解码会输出大量灰图/黑图\n  - 新增 `probe_video_codec()`：优先用 ffprobe 检测，回退 OpenCV FOURCC\n  - 新增 `_extract_ffmpeg()`：检测到 HEVC 编码时自动 fallback 到系统 ffmpeg 子进程抽帧\n  - 抽帧策略：跳过前 N 帧 + select 过滤器按帧间隔抽 + 后置灰图过滤\n  - GUI 新增"强制 ffmpeg 抽帧"开关（HEVC 视频必勾）\n  - CLI 新增 `--force-ffmpeg` 参数\n- **相似去重缓存性能优化**\n  - 落盘格式紧凑化：颜色直方图 float→int（0-255），phash→16 进制字符串，体积缩到 ~1/10\n  - 异步刷盘：`flush_async()` 把 IO 丢到后台线程，**不再阻塞特征提取**\n  - 刷盘阈值调大：50 张→200 张，30 秒→60 秒\n  - 兼容性：自动识别老版本 JSON 格式（float 直方图）+ 新版紧凑格式\n- **进程级异常兜底**\n  - 注册 `sys.excepthook` + `threading.excepthook`：进程任何位置抛出未捕获异常时，自动把活跃缓存刷盘\n  - 注册 `SIGINT`（Ctrl+C）处理器：用户中断也刷盘\n  - 子线程崩溃不再导致主线程 UI 永久卡死\n- **相似去重崩溃保护 + 特征缓存**\n  - `DedupeCache` 类把已抽取的图片特征持久化到 `<扫描目录>/.dedupe_cache.json`\n  - 原子写入（先写 `.tmp` 再 rename），断电/崩溃也不会出现半截 JSON\n  - 任务完成 / 主动停止 / 异常退出 时一律强制刷盘\n  - 完成得到结果后自动清理缓存文件\n- **缓存续扫弹窗**\n  - 再次扫描同一文件夹时检测到残留缓存，会弹出 3 选 1 弹窗（继续扫描/重新扫描/取消）\n- **GPU OOM 自动回退**\n  - `_find_similarity_gpu_feature_worker` 的矩阵乘法循环捕获 `OutOfMemoryError`\n  - 显存不足时自动按 1/2 子块重试，再不行推 CPU 算\n  - 实在不行时整体回退到 CPU pHash 模式\n- **抽帧灰图过滤**\n  - `_is_blank_frame` 增加 4 象限分块检测，识别上下/左右半灰等异常帧\n\n---\n\n## v1.0.9\n\n**日期**：2026-06-12\n**作者**：zhw\n\n### 修改内容\n\n- 新增新手引导教程（TutorialOverlay）\n- 首次启动自动弹出 8 步引导（介绍 → 导航 → 工具栏 → 浏览 → 抽帧 → 去重 → CLI → 完成）\n- 半透明全屏遮罩 + 焦点高亮目标区域\n- 步骤指示器（X / N）+ 跳过 / 上一步 / 下一步 / 完成按钮\n- 键盘快捷键：Esc 退出、← → 切换步骤\n- 引导标记 `tutorial_seen` 持久化到配置文件，避免重复弹出\n- 顶部菜单新增「❓ 帮助」按钮，可随时手动重启引导\n- CLI 帮助文档同步更新（描述引导功能）\n\n---\n\n## v1.0.8\n\n**日期**：2026-06-12\n**作者**：zhw\n\n### 修改内容\n\n- 将版本日志迁移到独立的 `VERSION_LOG.md` 文件（便于编辑维护）\n- CLI 新增 `-v` / `--version` 参数：输出最新版本信息\n- CLI 新增 `-v=all` / `--version=all` 参数：输出全部历史版本\n- CLI 新增 `-h` / `--help` 参数：列出所有现有功能说明\n- tk_app.py 顶部精简，仅保留 VERSION / AUTHOR / UPDATE_DATE 常量\n- 关于对话框的"版本日志"选项卡改为动态读取本文件\n\n---\n\n## v1.0.7\n\n**日期**：2026-06-12\n**作者**：zhw\n\n### 修改内容\n\n- 新增 Linux 平台兼容（路径规范化、ffmpeg 检测、可执行文件命名）\n- 新增 CLI 命令行模式（`python main.py --cli -i DIR -o DIR`）\n- 抽帧重构为并行架构（多视频同时处理 + 每视频内部多线程写入）\n- 新增子进度条：每个视频一个独立进度条 + 文件名 + 剩余时间\n- 新增总进度条聚合显示：总百分比、已用时间、剩余时间\n- 控制台模式使用 ANSI 光标控制实现动态刷新（避免输出混乱）\n- 新增剩余时间估算：统一 `HH:MM:SS` 格式\n- 修复相似去重任务停止后无法重新扫描的 bug\n- 新增软件介绍和版本日志区域（顶部"关于"按钮可查看）\n- CPU 版本打包保留 numpy（cv2 解码视频需要）\n\n---\n\n## v1.0.6\n\n**日期**：2026-05-20\n**作者**：zhw\n\n### 修改内容\n\n- 相似去重支持 GPU 多特征融合（颜色 + 纹理 + 形状）\n- 相似度阈值、精度模式可调（balanced / precise / fast）\n- 新增动态显存管理（OOM 自动降批次）\n- 任务支持暂停后继续（保存进度缓冲）\n- 修复若干内存泄漏问题\n\n---\n\n## v1.0.5\n\n**日期**：2026-04-15\n**作者**：zhw\n\n### 修改内容\n\n- 视频抽帧支持 GPU 加速（CUDA 批处理）\n- 新增抽帧任务可暂停 / 继续 / 停止\n- 新增多抽帧任务并行\n- 支持递归扫描子目录中的视频\n- 优化抽帧性能（多线程写入 PNG）\n\n---\n\n## v1.0.4\n\n**日期**：2026-03-08\n**作者**：zhw\n\n### 修改内容\n\n- 标签显示支持 YOLO segment 格式（多边形）\n- 标签显示支持 YOLO obb 格式（旋转框）\n- 新增 COCO JSON 加载与显示\n- 标签颜色根据类别自动分配\n\n---\n\n## v1.0.3\n\n**日期**：2026-02-10\n**作者**：zhw\n\n### 修改内容\n\n- 相似去重基础功能（pHash + 颜色直方图）\n- 支持按 pHash 汉明距离和颜色相似度双重筛选\n- 支持按组全选 / 智能选择（保留最高分辨率）\n\n---\n\n## v1.0.2\n\n**日期**：2026-01-15\n**作者**：zhw\n\n### 修改内容\n\n- 标记转移功能：批量移动 / 复制选中图片到目标目录\n- 支持保留目录结构或扁平化输出\n\n---\n\n## v1.0.1\n\n**日期**：2025-12-20\n**作者**：zhw\n\n### 修改内容\n\n- 数据浏览支持键盘快捷键（A/D/W/S 切换、Space 标记）\n- 缩略图缓存机制（LRU 淘汰）\n- 图片预加载优化\n\n---\n\n## v1.0.0\n\n**日期**：2025-11-01\n**作者**：zhw\n\n### 修改内容\n\n- 首版发布：数据浏览、标签显示、抽帧、相似去重\n'


@dataclass
class VersionEntry:
    version: str
    date: str
    author: str
    changes: list[str] = field(default_factory=list)


def _strip_md(s: str) -> str:
    """去除 Markdown 加粗标记。"""
    return re.sub(r"\*\*([^*]+)\*\*", r"\1", s).strip()


_VERSION_RE = re.compile(r"^##\s+(v[\d.]+(?:-[\w.]+)?)\s*$")
_DATE_RE = re.compile(r"\*\*日期\*\*[：:]\s*(\S+)")
_AUTHOR_RE = re.compile(r"\*\*作者\*\*[：:]\s*(\S+)")
_CHANGE_RE = re.compile(r"^[-*]\s+(.+)$")


def parse_version_log(md_path: str | Path | None = None) -> list[VersionEntry]:
    """解析 VERSION_LOG.md 文件为 VersionEntry 列表（按文件中顺序返回，最新在前）。"""
    if md_path is None:
        text = EMBEDDED_VERSION_LOG
    else:
        md_path = Path(md_path)
        if not md_path.exists():
            return []
        try:
            text = md_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = md_path.read_text(encoding="gbk", errors="replace")

    entries: list[VersionEntry] = []
    current: VersionEntry | None = None
    in_changes = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        m = _VERSION_RE.match(line)
        if m:
            if current is not None:
                entries.append(current)
            current = VersionEntry(version=m.group(1), date="", author="")
            in_changes = False
            continue
        if current is None:
            continue
        if not current.date:
            m = _DATE_RE.search(line)
            if m:
                current.date = _strip_md(m.group(1))
                continue
        if not current.author:
            m = _AUTHOR_RE.search(line)
            if m:
                current.author = _strip_md(m.group(1))
                continue
        if line.strip().startswith("### "):
            in_changes = "修改内容" in line or "改动" in line or "更新" in line
            continue
        if in_changes:
            m = _CHANGE_RE.match(line.strip())
            if m:
                current.changes.append(m.group(1).strip())
    if current is not None:
        entries.append(current)
    return entries


def get_latest(md_path: str | Path | None = None) -> VersionEntry | None:
    """获取最新版本。"""
    entries = parse_version_log(md_path)
    return entries[0] if entries else None


def get_all(md_path: str | Path | None = None) -> list[VersionEntry]:
    """获取所有版本。"""
    return parse_version_log(md_path)


def find_version_log_file() -> Path | None:
    """查找 VERSION_LOG.md 文件位置（支持打包后访问）。"""
    import sys

    # 1. 当前工作目录
    cwd = Path.cwd() / "VERSION_LOG.md"
    if cwd.exists():
        return cwd

    # 2. main.py 所在目录
    try:
        main_dir = Path(__file__).parent / "VERSION_LOG.md"
        if main_dir.exists():
            return main_dir
    except NameError:
        pass

    # 3. 打包后 _MEIPASS 临时目录（PyInstaller 解压路径）
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / "VERSION_LOG.md"
        if p.exists():
            return p
        p2 = Path(meipass) / "src" / "VERSION_LOG.md"
        if p2.exists():
            return p2

    # 4. 可执行文件自身所在目录
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent / "VERSION_LOG.md"
        if exe_dir.exists():
            return exe_dir

    return None


if __name__ == "__main__":
    # 单元测试
    p = find_version_log_file()
    print(f"Found: {p}")
    if p:
        entries = parse_version_log(p)
        print(f"Total versions: {len(entries)}")
        for e in entries[:3]:
            print(f"  {e.version} ({e.date} · {e.author}) - {len(e.changes)} changes")
