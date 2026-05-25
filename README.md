# 图片筛选工具 (Image Browser)

一款基于 Tkinter 的轻量级图片浏览和筛选工具，支持图片预览、缩放拖动、分类标记、批量复制/移动、视频抽帧、相似图片去重等功能。

## 功能特性

### 核心功能

#### 1. 数据浏览
| 功能 | 说明 |
|------|------|
| 图片预览 | 支持 jpg、jpeg、png、bmp、webp 等格式，可打开目录浏览 |
| 缩放拖动 | 鼠标滚轮缩放（以鼠标为中心），左键拖拽平移 |
| 上下张切换 | 工具栏按钮或键盘方向键切换 |
| 递归子目录 | 可选是否读取子目录中的图片 |
| 快速定位 | 状态栏滑块拖动定位，输入数字跳转到指定位置 |
| 进度保存 | 关闭后重新打开自动恢复到上次浏览位置 |

#### 2. 标签显示与编辑
| 功能 | 说明 |
|------|------|
| YOLO标签可视化 | 支持 detect/obb/segment/pose 四种标签绘制 |
| 类别过滤 | 可勾选显示/隐藏特定类别的标签 |
| 标签同步操作 | 图片复制/移动时自动同步同名标签文件 |
| 标签格式支持 | YOLO 格式（.txt），每行包含 class_id 和边界框/关键点坐标 |

#### 3. 标记转移
| 功能 | 说明 |
|------|------|
| 分类标记 | 可添加多个分类（如"合格"、"不合格"），点击标记 |
| 批量标记 | 支持批量操作，对选中图片进行标记 |
| 标记文件 | 标记后在图片所在目录生成对应的 txt 文件 |
| 复制/移动 | 将标记的图片复制/移动到指定目标目录 |
| 标签同步 | 图片复制/移动时自动同步处理标签文件 |

#### 4. 视频抽帧
| 功能 | 说明 |
|------|------|
| 视频选择 | 支持选择视频文件夹，批量处理 |
| 抽帧间隔 | 可设置按时间间隔（秒）或按帧数间隔抽帧 |
| 输出格式 | 支持 PNG 输出，可配置压缩级别 |
| 性能模式 | 可启用性能模式提高抽帧速度 |
| 进度显示 | 实时显示处理进度和已处理视频数 |

#### 5. 相似去重
| 功能 | 说明 |
|------|------|
| 双模式算法 | 支持 CPU 模式（pHash+颜色）和 GPU 模式（深度特征） |
| 参数调节 | CPU模式：pHash距离和颜色相似度阈值；GPU模式：仅颜色相似度 |
| GPU加速 | 支持 NVIDIA GPU 加速特征提取和相似度计算 |
| 智能聚类 | 自动将相似图片分组，支持查看和管理 |
| 缩略图缓存 | 加速大量图片的相似度结果显示 |

## 界面预览

程序采用多面板 Tab 式设计，主要包含：
- **数据浏览** - 图片浏览和基本操作
- **标签显示** - YOLO 标签可视化配置
- **标记转移** - 分类标记和批量操作
- **视频抽帧** - 视频转图片提取
- **相似去重** - 重复图片检测和清理

## 安装与运行

### 环境要求
- Python 3.8+
- Windows 10/11

### 依赖安装

```bash
# 创建 conda 环境（推荐）
conda create -n image_browser python=3.10
conda activate image_browser

# 安装核心依赖
pip install pillow opencv-python numpy imagehash torch

# 安装 tkinter（通常随 Python 自带）
# 如果使用的是 miniconda，可能需要额外安装
conda install tk
```

### 运行程序

```bash
# 进入项目目录
cd image_browser

# 直接运行
python main.py
```

## 打包为 exe

### 环境要求
- Python 3.8+
- Windows 10/11
- PyInstaller: `pip install pyinstaller`

### CPU 版本打包（推荐）

```bash
# 安装 PyInstaller
pip install pyinstaller

# 进入项目目录
cd image_browser

# 运行打包脚本
python build.py

# 输出文件: dist/ImageBrowser.exe
```

### GPU 版本打包（支持 CUDA 加速）

GPU版本打包需要更长的时间和更大的文件体积，但可以在目标机器上使用 NVIDIA GPU 进行加速。

#### 步骤 1：安装 PyTorch CUDA 版本

```bash
# 卸载 CPU 版本的 PyTorch（如果有）
pip uninstall torch torchvision

# 安装 CUDA 版本的 PyTorch（根据你的 CUDA 版本选择）
# CUDA 11.8 版本
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118

# 或者 CUDA 12.1 版本
# pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
```

#### 步骤 2：验证 GPU 支持

```python
# 测试 PyTorch 是否识别 GPU
python -c "import torch; print(f'CUDA available: {torch.cuda.is_available()}'); print(f'GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"None\"}')"
```

#### 步骤 3：打包

```bash
# 使用 --gpu 参数打包
python build.py --gpu

# 或者
python build.py -g

# 输出文件: dist/ImageBrowser.exe (GPU版本，体积约 2-3 GB)
```

### 注意事项

#### 文件体积
- **CPU 版本**: 约 50-100 MB
- **GPU 版本**: 约 2-3 GB（包含 CUDA 运行时库）

#### GPU 版本限制
- 目标机器必须安装 NVIDIA 显卡驱动
- 不支持 AMD 或 Intel 显卡
- 首次运行时会检测 CUDA 环境

#### 常见问题

**Q: 打包失败怎么办？**
A:
- 检查 PyInstaller 是否正确安装: `pip show pyinstaller`
- 清理缓存后重试: `rmdir /s /q build dist 2>nul & python build.py --gpu`
- 查看详细错误信息

**Q: GPU 版本在没有 NVIDIA 显卡的电脑上能用吗？**
A: **可以！** GPU 版本的 exe 会自动检测硬件环境：
- 如果有 NVIDIA GPU：自动使用 GPU 加速
- 如果没有 GPU 或 GPU 不可用：自动回退到 CPU 模式运行
- 程序会在启动时显示 "GPU可用：xxx" 或 "GPU不可用：原因"

**Q: GPU 版本和 CPU 版本有什么区别？**
A: 唯一的区别是文件体积。GPU 版本包含了 CUDA 库（约 2-3GB），而 CPU 版本没有（约 50-100MB）。功能上完全兼容，GPU 版本会自动适应硬件。

**Q: 如何减小 GPU 版本的体积？**
A:
- 使用 UPX 压缩: `pip install upyinstaller`
- 或者打包为文件夹而非单文件: 修改 build.py 移除 `--onefile` 参数

**Q: CPU 版本能用 GPU 吗？**
A: 不能，CPU 版本的 exe 不包含 CUDA 库，无法使用 GPU 加速。

### 发布建议

1. **普通用户**: 使用 CPU 版本（体积小，依赖少）
2. **专业用户**: 提供 GPU 版本链接，说明需要 NVIDIA 显卡
3. **分开发布**: 可以同时提供两个版本，让用户选择

## 使用说明

### 1. 数据浏览
1. 点击"打开图片目录"选择包含图片的文件夹
2. 使用工具栏按钮或键盘方向键切换图片
3. 滚动鼠标滚轮缩放，按住左键拖拽平移
4. 拖动底部滑块或输入数字快速定位

### 2. YOLO 标签可视化
1. 切换到"标签显示"面板
2. 点击"选择标签文件夹"选择包含 `.txt` 标签文件的目录
3. 点击"选择classes.txt"加载类别映射文件（可选）
4. 在下拉框中选择标签类型：**detect**、**obb**、**segment**、**pose**
5. 勾选"显示标签"或按 `T` 键切换标签显示
6. 在"类别过滤"中勾选需要显示的类别

### 3. 分类标记
1. 切换到"标记转移"面板
2. 在"标记分类"区域添加分类（如"合格"）
3. 点击分类按钮对当前图片进行标记
4. 标记后可在"批量操作"中复制/移动已标记的图片

### 4. 视频抽帧
1. 切换到"视频抽帧"面板
2. 点击"视频文件夹"选择包含视频的目录
3. 点击"输出文件夹"选择抽帧图片的输出目录
4. 设置抽帧间隔（秒或帧数）
5. 点击"开始抽帧"启动处理
6. 可同时运行多个视频任务

### 5. 相似去重
1. 切换到"相似去重"面板
2. 点击"图片文件夹"选择需要检测的目录
3. 设置相似度参数：
   - **CPU 模式**：pHash 距离（0-64）和颜色相似度（0-1）
   - **GPU 模式**：仅颜色相似度参数
4. 如需 GPU 加速，勾选工具栏"GPU加速"选项
5. 点击"扫描"开始检测
6. 扫描完成后点击"查看"浏览相似图片组
7. 选择需要删除的重复图片，点击"转移"进行清理

## 算法说明

### CPU 模式（pHash 算法）
- **pHash**：感知哈希算法，通过比较图像的哈希值判断相似度
  - pHash 距离越小，表示图片越相似
  - 距离 ≤ 2：直接判定为相似
  - 距离 2-阈值：结合颜色直方图综合判断
- **颜色相似度**：通过比较颜色直方图判断颜色相似程度
  - 值越大，表示颜色越相似（找出更多相似图片）

### GPU 模式（深度特征）
- 使用预训练的深度学习模型提取图像特征
- 通过余弦相似度计算图片相似度
- 仅使用颜色相似度参数（值越大，找出越多相似图片）
- pHash 参数在 GPU 模式下自动禁用

## 配置说明

配置文件位于 `~/.image_browser/config.json`，包含：
- 上次浏览目录和进度
- 分类标记列表
- 相似去重任务配置
- GPU 加速设置
- 性能模式设置

标记数据存储在图片所在目录的 txt 文件中（如"合格.txt"），每行一个绝对路径。

## 快捷键

| 快捷键 | 功能 |
|------|------|
| → / D | 下一张 |
| ← / A | 上一张 |
| F | 适应窗口 |
| T | 显示/隐藏标签层 |
| Ctrl+Z | 撤销 |
| Enter | 跳转到输入的数字位置 |

## 项目结构

```
image_browser/
├── main.py                 # 程序入口
├── requirements.txt        # 依赖声明
├── src/
│   ├── __init__.py
│   ├── tk_app.py          # 主应用程序（Tkinter）
│   ├── config.py          # 配置管理
│   ├── models/
│   │   ├── image_list.py  # 图片列表模型
│   │   ├── file_ops.py    # 文件操作模型
│   │   └── yolo_label.py  # YOLO 标签数据模型
│   └── utils/
│       ├── image_hash.py  # 图像哈希工具
│       └── similarity.py  # 相似度计算工具
├── config/                 # 配置文件目录
│   └── settings.json      # 用户配置
└── README.md
```

## 常见问题

**Q: 标记文件在哪里？**
A: 在图片所在目录下，文件名为分类名称（如"合格.txt"）。

**Q: 如何使用 GPU 加速？**
A: 确保已安装支持 CUDA 的 PyTorch 版本，并在工具栏勾选"GPU加速"选项。

**Q: GPU 模式和 CPU 模式有什么区别？**
A: CPU 模式使用传统的 pHash 算法，可以调节 pHash 距离和颜色相似度；GPU 模式使用深度学习特征，仅需调节颜色相似度，通常更快但只使用颜色信息。

**Q: 相似去重的参数如何调节？**
A: CPU 模式下，pHash 距离越小越严格，颜色相似度越大越宽松；GPU 模式下，颜色相似度越大，找出的相似图片越多。

**Q: 批量操作时标签会同步吗？**
A: 会。图片复制/移动时会自动查找并同步处理同名 `.txt` 标签文件。

**Q: 撤销能恢复被移动的文件吗？**
A: 可以，撤销会将文件移回原目录。

## 技术栈

| 包名 | 用途 |
|------|------|
| tkinter | 窗口、控件、布局（Python 内置） |
| Pillow | 图片打开、格式转换 |
| opencv-python | 视频处理 |
| numpy | 数值计算 |
| imagehash | 图像哈希计算 |
| torch | GPU 加速（可选） |
| torchvision | 深度学习模型（GPU 模式可选） |

## License

MIT License
