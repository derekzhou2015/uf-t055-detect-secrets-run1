# 交付说明：CLI 与库入口漏报对齐修复（uf-t055）

## 1. 问题现象

同一套检测内核，命令行入口（`detect-secrets scan`）与库入口
（`SecretsCollection` / `scan.scan_file`）对同一文件给出不同结果：库入口能报出的
若干命中，在命令行结果中静默消失，全程无任何告警，密钥台账因此缺项。

排查确认这是**两个互相独立、各自都会造成静默漏报的根因**叠加。

## 2. 根因

### 根因 A：非 UTF-8 locale 下整文件被静默丢弃（编码问题）

- 扫描读取文件用的是不带 `encoding` 的内建 `open()`，在中文 Windows 上其默认编码为
  `cp936/GBK`（实测 `locale.getpreferredencoding(False) == 'cp936'`）。
- 含非 ASCII 字符的 UTF-8 文件（如 `test_data/config.md` 中的制表符 `╭ ╰ »`、
  带中文注释的源码/配置）在读取时抛 `UnicodeDecodeError`。
- `_get_lines_from_file` 把 `UnicodeDecodeError` 一律当作“二进制文件”处理并 `return`，
  于是**整个文件被跳过，里面所有真实密钥一并消失且没有任何提示**。
- 雪上加霜的是：`transformers.get_transformed_file` 只捕获 `ParsingError`，
  transformer 内部抛出的 `UnicodeDecodeError` 会直接穿透，连“回退到按原始行扫描”的
  兜底路径都到不了。
- 这正是基线测试 `test_file_based_success_unexpected_config_file[config.md]` 与
  `TestSlimScan::test_restores_line_numbers` 在本机失败的原因。

### 根因 B：CLI 比库入口多挂了一个默认过滤器（装配分歧）

- 两个入口共用同一扫描内核，但过滤器是各自装配的。
- 库入口（`default_settings()` / `transient_settings()`）只启用内建默认过滤器。
- 命令行入口在 `core/usage/filters.py` 中，只要环境里装了可选依赖
  `gibberish-detector`，就会**自动**把
  `detect_secrets.filters.gibberish.should_exclude_secret` 挂上去（无需用户指定任何
  参数）。该过滤器会把“不像乱码”的高熵串（含部分真实密钥形态）判为假阳性剔除。
- 于是“装没装某个可选包”这一与扫描意图无关的环境差异，导致 CLI 比库入口**少报**，
  且没有任何提示。

## 3. 修复方案与选型

### 修复 A：文本读取统一为“UTF-8 优先、locale 回退”

新增 `detect_secrets/util/encoding.py`：

- `get_preferred_text_encodings()`：依次给出 `utf-8` 与平台 locale 编码（去重、规范化）。
- `read_text_lines()`：按上述顺序逐个编码尝试读取整个文件；成功则返回已 `seek(0)`
  的句柄和行内容；所有文本编码都失败才抛 `UnicodeDecodeError`。

`core/scan.py::_get_lines_from_file` 改用它，并保留原有语义：

- 真正的二进制文件（任何文本编码都解不开）依旧抛 `UnicodeDecodeError`，被原有的
  “忽略二进制文件”分支吞掉——**既有的二进制忽略防误报机制原样保留**。
- UTF-8 文件在 GBK 机器上不再被误判为二进制；老式 locale 编码文件仍能靠回退读到。

`transformers/__init__.py::get_transformed_file` 调整异常处理：

- 单个 transformer 解码失败不再让整个文件夭折，而是继续尝试其它 transformer；
- 全部可用 transformer 都解码失败时再重新抛出，交给上层统一的二进制处理，
  杜绝“静默返回空、文件凭空消失”。

**为什么选 UTF-8 优先而不是只用 locale 编码**：现代源码/配置绝大多数是 UTF-8；
以 locale 为单一口径在非 UTF-8 机器上必然漏报。UTF-8 优先 + locale 回退在召回率与
向后兼容间取得平衡。

### 修复 B：gibberish 过滤器改为显式 opt-in，消除入口装配分歧

- 新增命令行开关 `--use-gibberish`。
- 默认情况下 CLI 不再因“恰好装了可选依赖”就自动挂 gibberish 过滤器，
  使 **CLI 与库入口的默认过滤器集合完全一致**，从根上保证“CLI 不得漏报库入口能报的命中”。
- 防误报能力**没有被删除**：
  - 显式 `--use-gibberish` 仍启用该过滤器；
  - 提供 `--gibberish-model` / `--gibberish-limit` 也视为显式启用（这两个参数本就只有在
    过滤器生效时才有意义）；
  - 经由 baseline 载入的 `filters_used` 配置路径（`configure_settings_from_baseline`）
    完全不变，历史台账中的 gibberish 配置继续生效；
  - 过滤器实现本身、其单元测试一律未改动。

## 4. 牺牲与权衡（明确交代）

1. **gibberish 默认行为变更（有意的召回/精度取舍）**：默认不再自动启用 gibberish
   过滤，意味着此前被它静默压制的那部分“词样/URL 样”高熵假阳性，默认扫描会重新报出，
   噪声可能上升。这是为满足“CLI 不得漏报库入口能报的命中”而选择的**优先保证召回**。
   需要原精度的用户显式加 `--use-gibberish` 即可，能力无损。
2. **文本编码判定**：统一改为 UTF-8 优先。极少数既非 UTF-8、又与平台 locale 编码不符的
   老式文本文件，仍会落入二进制忽略分支（与历史行为一致），不做激进的编码嗅探，
   以避免猜测编码带来的误判和复杂度。
3. **读取方式**：`read_text_lines` 会先整文件读一遍以确定可用编码，再交给 transformer/
   原始行扫描，相对原先的惰性 `open` 多一次读取。换来的是编码回退的确定性；对密钥扫描
   这种 I/O 非瓶颈场景可接受。

## 5. 防误报机制的去向（对照清单，未静默移除）

| 既有机制 | 处理 |
| --- | --- |
| 二进制文件忽略（`UnicodeDecodeError` 分支） | 保留，且由回归测试 `test_handles_binary_files_gracefully` 持续守护 |
| 非文本扩展名忽略 `is_non_text_file` | 未改动 |
| 各类启发式过滤器（顺序串/UUID/模板/间接引用/锁文件/swagger 等） | 未改动 |
| allowlist pragma、regex exclude、wordlist | 未改动 |
| 网络验证策略过滤器 `is_ignored_due_to_verification_policies` | 未改动；`--no-verify` 仍按原逻辑关闭它，断网口径不变 |
| gibberish 过滤器 | 实现保留、测试保留；仅由“默认隐式启用”改为“显式 opt-in / baseline 仍可启用” |

## 6. 验证口径与结果

- 全程**断网**口径验证（设置无效代理 `HTTP(S)_PROXY=127.0.0.1:9`），扫描一律带
  `--no-verify`，不产生任何网络依赖。
- 同一文件连跑 3 次，去除时间戳后输出哈希完全一致（确定性）。
- CLI 与库入口对 `config.md` / `config.env` / `config.yaml` 等样本结果逐一相等，
  CLI 不再缺项。
- 新增最小回归集 `tests/core/test_t055_regression.py`（7 个用例）：
  - 强制 `cp936` locale 下 UTF-8 文件仍扫得到密钥（复现根因 A 的最小集合）；
  - GBK 老式编码文件经回退仍可扫；
  - 重复扫描确定性；
  - CLI 默认不隐式挂 gibberish、且不漏报库入口命中；
  - `--use-gibberish` 与 `--gibberish-model` 仍能显式启用该过滤器。
- 原有两个在本机失败的基线用例（config.md 相关）修复后转为通过；既有测试整体保持通过；
  `flake8 --max-line-length=100` 与 `mypy` 对改动文件均干净。

## 7. 改动文件

- 新增 `detect_secrets/util/encoding.py`
- 修改 `detect_secrets/core/scan.py`
- 修改 `detect_secrets/transformers/__init__.py`
- 修改 `detect_secrets/core/usage/filters.py`
- 新增 `tests/core/test_t055_regression.py`
- 修改 `tests/core/secrets_collection_test.py`（mock 目标随读取入口更名）
