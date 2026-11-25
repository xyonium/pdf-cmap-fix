# pdf-cmap-fix

自动检测并修复 **缺失或损坏 `/ToUnicode` CMap** 的 PDF（典型如 SVG/排版工具做字体子集压缩嵌入时丢失 CID→Unicode 映射，导致阅读正常但复制/搜索全变乱码的电子杂志与出版物）。

## 核心特性

1. **自动检测（`check`）**：毫秒级扫描 PDF 中所有 `/Type0` (`/Identity-H` / `/Identity-V`) 字体，校验 `/ToUnicode` 是否缺失、为空、存在未覆盖的真实 CID，或包含会导致 Adobe Reader 粘贴变方块（□□□）的非法 `bfrange` 跨界条目。
2. **合成白底矢量重放（Synthetic Vector Replay）**：
   - 很多缺失 CMap 的 PDF 字体同时写着 `/FontBBox [0 0 0 0]`，直接从原页裁剪截图容易偏移、小字模糊、受背景图干扰，且遇到 90° 旋转文字时普通 OCR 极易失效。
   - 本工具直接引用该 PDF 内部嵌入的原始矢量字体（`font_xref`），在内存 O(1) 临时白底页上以 **48pt / 144 DPI 纯黑水平矢量重放**（超长行自动按字宽分块），彻底根治偏移、模糊与 90° 旋转问题；仅在非 `Identity-H` 等特殊字体时优雅回退到 `rawdict` 精确字符包围盒裁剪。
3. **严格兼容 Adobe Reader / Edge / Preview 的 ToUnicode 生成器**：
   - 遵循 **ISO 32000-1 §9.10.3** 与 **Adobe Technical Note #5411**：默认输出规范 `bfchar` 块（每块 ≤ 100 条），彻底避免 `bfrange` 跨 CID 高字节（`0xXXFF → 0x(XX+1)00`）或目标 UTF-16BE 末字节 `0xFF` 溢出导致的 Adobe Reader 粘贴变方块；
   - 标准 `/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >>` 与 `CMapName currentdict /CMap defineresource pop`；
   - 干净流字典（无非标准 `/Type /Stream`），按 CMap 内容 SHA-256 共享流对象，保存时启用 `deflate=True, garbage=3` 避免文件膨胀，并自动兼容 `pdfcpu` 合并产生的残缺 xref 表。
4. **增量学习缓存（v2 格式）**：
   - 使用紧凑 UTF-8 频次字典（`{"counts": {"责": 48}, "locked": true}`）保留全部高/低频候选及计数，相比旧版重复整数列表体积缩小约 90%，原生支持 BMP、非 BMP 代理对、`fi`/`ffi` 连字与组合字符；
   - 自动识别并隔离按子集重编号 CID 的字体（如 `ArialMT`、`CIDFontType2` + 流式 `/CIDToGIDMap`），防止污染全局字体族缓存；
   - 同源杂志后续新刊通常 **98%～99% CID 直接命中缓存**，仅需对极少数未见新字调用视觉 API。

---

## 安装

推荐使用 [`uv`](https://docs.astral.sh/uv/)（Python ≥ 3.10）：

```bash
git clone https://github.com/xyonium/pdf-cmap-fix.git
cd pdf-cmap-fix
uv sync
```

或使用 `pip`：

```bash
pip install -e .
# 如需本地 Tesseract 回退：
pip install -e ".[tesseract]"
```

---

## API 与模型配置建议

OCR 后端支持任意 **OpenAI 兼容多模态视觉接口**，**支持命令行参数与环境变量二选一**（命令行参数优先级更高）：

| 配置项 | 命令行参数 | 环境变量（按优先级） |
|---|---|---|
| API Key | `--api-key` | `PDF_CMAP_API_KEY` → `DASHSCOPE_API_KEY` → `OPENAI_API_KEY` |
| Base URL | `--base-url` | `PDF_CMAP_BASE_URL` → `OPENAI_BASE_URL`（默认 DashScope 兼容端点） |
| 模型名 | `--model` | `PDF_CMAP_MODEL` |

> **免费额度与模型选择提示**：
> - 许多兼容 OpenAI 协议的平台会提供免费试用额度（例如阿里云百炼 DashScope 等经常对新上线的视觉/多模态旗舰模型提供 1M Token 免费额度），可先注册领取免费额度，再通过 `--base-url` 与 `--model` 切换到**足够先进的新一代多模态视觉模型**（请勿沿用过时的老版本模型）。
> - 由于部分老模型或小模型容易误认生僻汉字与全角标点，**批量运行前强烈建议先用包含生僻字（如「小䴙䴘」）的测试页验证当前所选模型的识别准确率**，确认无误后再跑整本 PDF。
> - 当待修复 PDF 的 CID 已 100% 命中本地缓存（或使用 `--no-ocr` / `--cmap-dir`）时，无需配置任何 API Key 或模型即可完全离线运行。

---

## 开源参考预设（`presets/`）

仓库内置了一份通用字体的 v2 映射参考预设：[presets/zinio-sourcehan-v2.json](presets/zinio-sourcehan-v2.json)（包含 `SourceHanSansCN` 思源黑体与 `SourceHanSerifCN` 思源宋体约 8,200+ 个 CID 映射）。

> **重要说明（Tool-Specific 提醒）**：  
> PDF 内部的 CID 编号空间**可能与生成该 PDF 的具体排版/SVG 转换工具强相关**，**不保证** `presets/zinio-sourcehan-v2.json` 一定能直接对应你手中任意来源的 PDF！  
> 如果你的 PDF 来自相同出版/转换管线（例如同系列 Zinio 订阅中文刊物），可将其复制为本地种子缓存大幅减少 OCR 运算量；如果是其他工具生成的 PDF，**务必先单页抽检复制结果**，若映射不一致请从空白缓存文件重新积累，切勿盲目混用。

---

## 快速上手

### 1. 检测 PDF 是否需要修复

```bash
# 单文件或目录检测（退出码：0=健康, 2=需要修复, 1=错误）
uv run pdf-cmap-fix check input.pdf
uv run pdf-cmap-fix check ./issues/ --cache ./data/cmap_cache.json --json

# 额外严格检查旧工具生成的非法 Adobe Reader bfrange 跨界问题
uv run pdf-cmap-fix check input.pdf --strict-cmap
```

### 2. 自动检测并修复 PDF

```bash
# 配置环境变量（或改用 --api-key / --base-url / --model 参数）
export PDF_CMAP_API_KEY="your-api-key"
export PDF_CMAP_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export PDF_CMAP_MODEL="your-modern-vision-model"

# 修复单本 PDF（仅在需要时执行，复用并增量更新本地缓存）
uv run pdf-cmap-fix fix input.pdf -o fixed/input.pdf \
  --cache ./data/cmap_cache.json \
  --if-needed

# 原位替换（原子写回；若希望任何未映射字都中止而不写回，可加 --strict）
uv run pdf-cmap-fix fix input.pdf --in-place --cache ./data/cmap_cache.json --if-needed

# 批量处理整个目录
uv run pdf-cmap-fix fix ./issues/ -o ./fixed/ --cache ./data/cmap_cache.json --if-needed

# 纯离线模式（不调用 API，仅用已有缓存嵌入；有未映射字时返回退出码 3）
uv run pdf-cmap-fix fix input.pdf -o fixed/input.pdf --cache ./data/cmap_cache.json --no-ocr

# 强制用严格 Adobe Reader 兼容格式重打已有 /ToUnicode 的旧 PDF（例如修复旧版在 Adobe Reader 粘贴变方块的问题）
uv run pdf-cmap-fix fix old_fixed.pdf -o adobe_ok.pdf --cache ./data/cmap_cache.json --force --no-ocr
```

### 3. 缓存统计、冲突审查与维护

```bash
# 查看缓存统计与各字体冲突率
uv run pdf-cmap-fix cache stats ./data/cmap_cache.json

# 从旧版 v1 列表格式无损迁移到 v2 频次表（可选修复旧版 SourceHan CID 32 问号污染）
uv run pdf-cmap-fix cache migrate legacy_cid_to_unicode.json ./data/cmap_cache.json --clean-sourcehan-cid32

# 查看存在多候选竞争的 CID（按最接近平局排序）
uv run pdf-cmap-fix cache conflicts ./data/cmap_cache.json --font SourceHanSansCN

# 人工修正单个映射并锁定（锁定后不再被后续 OCR 覆写）
uv run pdf-cmap-fix cache set ./data/cmap_cache.json SourceHanSansCN 32 "?"

# 将高置信条目提升为 locked 并按比例封顶超大计数（不丢弃低频候选记录）
uv run pdf-cmap-fix cache compact ./data/cmap_cache.json

# 导出各字体的标准 .cmap 文件供人工审查或离线复用
uv run pdf-cmap-fix cache export-cmap ./data/cmap_cache.json -o ./exported_cmaps
```

### 退出码约定

| 命令 | 退出码 | 含义 |
|---|---:|---|
| `check` | `0` | 全部 PDF 健康，无需修复 |
| `check` | `2` | 检测到缺失/无效/不完整 ToUnicode，需要修复 |
| `check` | `1` | 参数或读取错误 |
| `fix` | `0` | 全部修复完成（或 `--if-needed` 跳过） |
| `fix` | `3` | 部分修复（已写入可用映射，但仍有少量未映射 CID；下次 `check` 仍会报需修复） |
| `fix` | `1` | 鉴权/配置/IO 错误或 `--strict` 拦截（**不会**写入残缺 CMap） |

---

## 与 ZiniGo 自动联动

在同级工作区部署 `ZiniGo/` 与 `pdf-cmap-fix/` 后，`ZiniGo` 合并完每期 PDF 会自动检测并按需调用 `pdf-cmap-fix`：

```bash
# ZiniGo 默认 -fix-cmap=auto：发现工具即自动检测修复；未配 API Key 时仅告警不丢原件
./ZiniGo -fix-cmap=auto -cmap-cache=../data/cmap_cache.json

# 若希望保留原始 PDF 在 issue/，修复版输出到 fixed/
./ZiniGo -fix-cmap=on -cmap-cache=../data/cmap_cache.json -cmap-out=fixed
```

也可在 `ZiniGo` 的 `config.json` 中持久化 `cmap_mode`、`cmap_tool`、`cmap_cache`、`cmap_out` 字段。

## License

MIT
