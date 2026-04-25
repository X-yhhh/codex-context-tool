# Codex Context Tool

一个可移植的 Codex 上下文配置工具，用于安全地查看、应用和移除
Codex 模型上下文窗口覆盖配置。

工具只会修改 Codex `config.toml` 根层级中它负责的 3 个 key：

- `model_context_window`
- `model_auto_compact_token_limit`
- `model_catalog_json`

它还会写入或复用一个本地模型 catalog，并把目标模型的
`context_window` 和 `max_context_window` 更新为你手动指定的窗口大小。
窗口最大支持 `1000000`，但推荐使用时显式传入你想要的值，而不是直接依赖默认值。

## 适用场景

- 想把 Codex 客户端或 CLI 的模型上下文窗口调整到指定大小。
- 想在不同机器、不同用户目录、不同 Codex 安装方式下复用同一套工具。
- 想修改前先预览 diff，并在修改前自动备份原始配置。
- 想随时清除覆盖配置，回到 Codex 官方模型 metadata。

## 从 0 到 1 使用教程

下面流程假设你是第一次把项目拉到本地。所有命令都在项目根目录执行。

### 1. 拉取项目

```bash
git clone <repo-url>
cd codex-context-tool-0.2.0
```

如果你已经下载了解压包，直接进入解压后的项目目录即可。

### 2. 确认 Python 可用

工具只依赖 Python 标准库，不需要安装第三方包。

```bash
python3 --version
```

要求 Python `3.10` 或更高版本。

### 3. 运行自检测试

测试只使用临时目录中的配置文件，不会修改你真实的
`~/.codex/config.toml`。

```bash
python3 -m unittest
```

看到类似下面的结果即可继续：

```text
Ran 11 tests

OK
```

### 4. 选择运行方式

可以不安装，直接从项目目录运行：

```bash
./bin/codex-context-tool --help
```

也可以安装成本机命令：

```bash
python3 -m pip install -e .
codex-context-tool --help
```

后续示例使用 `codex-context-tool`。如果你没有安装，请把命令替换为
`./bin/codex-context-tool`。

### 5. 查看当前 Codex 配置状态

先只读查看，不会修改任何文件：

```bash
codex-context-tool status
```

工具默认读取：

```text
~/.codex/config.toml
```

如果你的 Codex 配置不在默认位置，可以显式指定：

```bash
codex-context-tool --config /path/to/config.toml status
```

### 6. 手动决定上下文窗口大小

不要直接盲目使用最大值。先按你的机器、模型和使用场景选择一个不超过
`1000000` 的值。

常见示例：

- `300000`：较保守，适合普通代码阅读和轻量任务。
- `500000`：中等窗口，适合较大的项目分析。
- `750000`：较大窗口，适合长上下文代码审查或迁移任务。
- `1000000`：最大值，只在明确需要时使用。

窗口值通过 `--context-window` 手动设置。这个全局参数必须放在子命令
`apply` 前面：

```bash
codex-context-tool --context-window 750000 apply --dry-run
```

### 7. 预览修改，不直接写入

第一次使用一定建议先 dry-run。

```bash
codex-context-tool --context-window 750000 apply --dry-run
```

如果你想同时手动设置 auto-compact 阈值，也可以显式传入：

```bash
codex-context-tool \
  --context-window 750000 \
  --auto-compact-token-limit 675000 \
  apply --dry-run
```

如果不传 `--auto-compact-token-limit`，工具会按上下文窗口的 90% 自动计算。
例如 `750000` 会得到：

```toml
model_context_window = 750000
model_auto_compact_token_limit = 675000
```

### 8. 确认无误后应用

确认 dry-run 输出符合预期后，再真正写入配置：

```bash
codex-context-tool --context-window 750000 apply
```

或手动指定 compact 阈值：

```bash
codex-context-tool \
  --context-window 750000 \
  --auto-compact-token-limit 675000 \
  apply
```

执行时工具会：

1. 读取你的本地 Codex 配置。
2. 自动复用本地已有的 `model`。
3. 自动复用本地已有的 `model_catalog_json`，如果没有则写到用户目录下的默认位置。
4. 生成修改前备份。
5. 写入新的上下文窗口配置。
6. 记录本次变更历史。

### 9. 重新查看状态

```bash
codex-context-tool status
```

重点确认这些值：

```text
model_context_window
model_auto_compact_token_limit
model_catalog_json
resolved catalog <model>.context_window
resolved catalog <model>.max_context_window
```

### 10. 重启 Codex 或新开线程

配置写入后，通常需要新开 Codex 线程或重启 Codex 客户端，运行时行为才会完全
使用新的上下文窗口。

## 使用显式 catalog 文件

如果当前机器无法运行 `codex debug models --bundled`，可以手动提供一个源
catalog 文件：

```bash
codex-context-tool \
  --config ~/.codex/config.toml \
  --catalog ~/.config/codex-context-tool/catalog/my-model-catalog.json \
  --model gpt-5.5 \
  --context-window 750000 \
  --auto-compact-token-limit 675000 \
  apply --source-catalog ./model-catalog.json
```

这里的 `--source-catalog` 是读取来源，`--catalog` 是写入后的本地 patched
catalog 位置。

## 恢复与清理

移除本工具写入的 3 个 root key，并回到 Codex 官方 metadata：

```bash
codex-context-tool clear
```

`clear` 不会删除本地 patched catalog 文件，但会移除 `model_catalog_json`，所以该
catalog 会变成 inert 状态，不再被 Codex 使用。

恢复某次备份中的完整 `config.toml`：

```bash
codex-context-tool restore --backup ~/.local/state/codex-context-tool/backups/config.toml.YYYYmmdd-HHMMSS.bak
```

## 路径与优先级

CLI 参数优先级最高，其次是环境变量，然后是本地 Codex 配置，最后才是工具内置
兜底值。

- Config: `--config`，然后 `CODEX_CONTEXT_CONFIG`，然后 `~/.codex/config.toml`
- Model: `--model`，然后 `CODEX_CONTEXT_MODEL`，然后本地 root `model`，最后 `gpt-5.5`
- Context window: `--context-window`，然后 `CODEX_CONTEXT_WINDOW`，然后本地 root `model_context_window`，最后 `1000000`
- Auto-compact limit: `--auto-compact-token-limit`，然后 `CODEX_CONTEXT_AUTO_COMPACT_TOKEN_LIMIT`，然后在复用本地窗口时读取 root `model_auto_compact_token_limit`，否则使用所选窗口的 90%
- Patched catalog: `--catalog`，然后 `CODEX_CONTEXT_CATALOG`，然后本地 root `model_catalog_json`，最后 `$XDG_CONFIG_HOME/codex-context-tool/catalog/<model>-model-catalog.json` 或 `~/.config/codex-context-tool/catalog/<model>-model-catalog.json`
- State directory: `--state-dir`，然后 `CODEX_CONTEXT_STATE_DIR`，然后 `$XDG_STATE_HOME/codex-context-tool` 或 `~/.local/state/codex-context-tool`
- Codex CLI: `--codex-bin`，然后 `CODEX_BIN`，然后 `codex` from `PATH`，最后尝试 macOS app fallback

## 常用命令速查

```bash
# 只读查看
codex-context-tool status

# 手动设置 500000 窗口并预览
codex-context-tool --context-window 500000 apply --dry-run

# 手动设置 500000 窗口并应用
codex-context-tool --context-window 500000 apply

# 手动设置窗口和 compact 阈值并预览
codex-context-tool \
  --context-window 500000 \
  --auto-compact-token-limit 450000 \
  apply --dry-run

# 清除覆盖配置
codex-context-tool clear
```

兼容别名：

- `workaround` 是 `apply` 的别名。
- `official` 是 `clear` 的别名。
- `bin/codex-gpt55-context` 会调用新的通用 CLI。

## 安全说明

- `status` 是只读命令。
- `apply`、`clear`、`restore` 写入前都会创建配置备份。
- `--dry-run` 只打印计划修改，不写入文件。
- `clear` 只移除本工具负责的 3 个 root key。
- 运行历史写入用户 state 目录，不写入项目 checkout。
- 单元测试只使用临时配置文件，不会修改真实 Codex 配置。
