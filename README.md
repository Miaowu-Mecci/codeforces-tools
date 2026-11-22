# Codeforces Tools

用命令行向 Codeforces 普通赛和 Gym 提交 C++ 源码。可以在 Ubuntu、WSL 或远程服务器上写题，借助本机 Chrome 或 Edge 中已经登录的 Codeforces 会话完成提交。

```bash
cflogin --browser
cfset contest 2288
submit --dry-run A.cpp
submit A.cpp
```

- `cflogin` 登入网站会话。浏览器模式由你在网页登录和完成验证，会话保留在浏览器里。
- `cfset` 保存当前比赛并缓存题目列表。公开普通赛优先从官方 API 读取，Gym 通过网页会话获取。
- `submit` 根据文件名识别题号，显示题目与编译器，确认后发送源码并报告提交 ID。只接受提交页中唯一可用的 GNU G++23 选项。

## 安装 CLI

要求 Linux 和 Python 3.10+。Ubuntu 缺少虚拟环境组件时，先安装 `python3-venv`。

```bash
git clone https://github.com/Miaowu-Mecci/codeforces-tools.git
cd codeforces-tools
python3 -m venv .venv
.venv/bin/python -m pip install .
```

在当前终端启用命令：

```bash
source .venv/bin/activate
```

希望在任意目录直接运行三个命令，可把它们链接到 `~/.local/bin`：

```bash
mkdir -p ~/.local/bin
ln -s "$PWD/.venv/bin/cflogin" ~/.local/bin/cflogin
ln -s "$PWD/.venv/bin/cfset" ~/.local/bin/cfset
ln -s "$PWD/.venv/bin/submit" ~/.local/bin/submit
export PATH="$HOME/.local/bin:$PATH"
```

已有同名命令时，`ln -s` 会报错。可以继续使用虚拟环境里的命令。若新终端找不到命令，把最后一行加入 shell 的启动配置。

## 浏览器登录

浏览器扩展要求 Chrome 或 Edge（Chromium 120+）。在运行浏览器的电脑上准备仓库里的 [browser_extension](browser_extension) 目录。如果只在远程服务器上克隆了项目，可以在本机通过 GitHub 的「Code → Download ZIP」下载整个仓库，解压后找到该目录。

1. 打开 `chrome://extensions` 或 `edge://extensions`，开启开发人员模式。
2. 点「加载已解压的扩展程序」，选择包含 `manifest.json` 的 `browser_extension` 目录。
3. 根据下面的环境选择连接方式。在 CLI 所在的终端运行 `cflogin --browser`。
4. 打开浏览器工具栏里的 **CF Submit Browser**，核对「本机连接端口」，输入终端显示的配对码并点「连接」。
5. 在扩展打开的 Codeforces 页面手动登录和验证。终端显示 `[OK] Signed in as ...` 后，就可以运行 `cfset` 和 `submit`。

如果担心网页登录到另一个账号，可以显式核对账号：

```bash
cflogin --browser my_handle
```

扩展打开的 Codeforces 标签页可以关闭。执行需要浏览器的命令时，保持同一个浏览器配置开启。扩展通常在 30 秒内领取任务，也可以点击「立即检查」。空闲时可以关闭浏览器和 SSH 连接，下次使用时重新打开即可。Codeforces 会话过期后，重新运行 `cflogin --browser`。

### 本机 Ubuntu 或 WSL

CLI 在本机 Ubuntu 时，扩展直接连 `127.0.0.1:12450`。如果 CLI 在 WSL 中，通常可从 Windows 的浏览器通过同一个地址连接。相同端口上的旧 SSH 转发会占用连接，改用本机 WSL 时请先关掉旧转发，或选一个新的端口。[微软 WSL 网络文档](https://learn.microsoft.com/en-us/windows/wsl/networking#accessing-linux-networking-apps-from-windows-localhost)

有图形会话的 Ubuntu 可用 `xdg-open` 自动打开默认浏览器：

```bash
cflogin --browser --open-browser
```

使用前请确认默认浏览器是已加载扩展的 Chrome 或 Edge。没有图形会话的环境可以用下方的远程服务器方案。

### 远程服务器

CLI 在服务器，扩展在你电脑上的浏览器。先在**本机终端**建立 SSH 本地转发，并保持会话开启：

```bash
ssh -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:12450:127.0.0.1:12450 user@server
```

如果服务器用自定义 SSH 端口，在 `ssh` 后加 `-p SSH_PORT`。连接后在服务器终端运行 `cflogin --browser`，扩展保持默认的本机端口 `12450`。

### 自选端口

```bash
cflogin --browser --port 12452
```

本机 Ubuntu 和 WSL 通常把扩展的「本机连接端口」也设为 `12452`。远程服务器可用不同的本机和服务器端口。例如下面的命令把本机 `12453` 连到服务器 `12452`：

```bash
ssh -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:12453:127.0.0.1:12452 user@server
```

此时扩展填 `12453`，服务器运行 `cflogin --browser --port 12452`。登录成功后，CLI 会记住服务器端口，扩展也会记住所选本机端口。切换到另一个 CLI 环境时，在扩展里点「断开」，再输入新环境的配对码。

### 直接 HTTP 登录

`cflogin my_handle` 通过隐藏输入读取密码，登录成功后会保存网页 Cookie 并切换到 HTTP 模式。Codeforces 返回 Cloudflare 验证时，请使用 `cflogin --browser` 在网页完成验证。

## 设置比赛与提交

```bash
cfset contest 2288     # 设置普通赛
cfset gym 104172       # 设置 Gym
cfset refresh          # 重新抓取当前比赛题表
```

`cfset` 只有在读到完整题表后才更新配置和缓存。普通赛如果无法从公开 API 获取题表，CLI 会尝试有权限的比赛主页。私有比赛、练习和组队权限由 Codeforces 管理。平时运行 `submit` 时，题目名称从缓存读取。

```bash
submit A.cpp
submit b2.cpp -y
submit main.cpp -p C
submit --dry-run A.cpp
submit --debug A.cpp
```

- 源码应为 UTF-8。文件名形如 `A.cpp`、`B1.cpp` 或 `b2.cpp` 时，会自动识别题号。文件名不含题号时，可用 `-p` 指定。
- 默认显示题目名称、编译器和源码 SHA-256。输入 `Y/y` 提交，输入 `N/n` 或回车取消。`-y` 跳过询问，仍会执行检查。
- `--dry-run` 会校验账号、题目、表单和语言，不发送源码提交。确认后如果又修改了源码，CLI 会停止当前提交。
- `Submission Accepted` 表示网站接收了源码。最终评测结果可以在返回的详情链接里查看。

## 常见问题

| 提示或现象 | 处理办法 |
| --- | --- |
| `Browser did not connect` | 确认浏览器仍在运行，扩展已配对，端口设置正确。远程环境还需保持 SSH 转发。 |
| `cfset gym` 或 `submit` 在等待浏览器 | 扩展会在后台轮询。需要立即检查时，点击扩展里的「立即检查」。 |
| 扩展无法识别已登录账号 | 在装有扩展的同一个浏览器中打开 [Codeforces 账号设置](https://codeforces.com/settings/general)。确认扩展已获准访问 `codeforces.com`，再运行 `cflogin --browser`。 |
| `Submission Status Unknown` | 先打开提示里的个人提交记录确认是否已接收。公开普通赛在回执不完整时会只读查询官方 API，只有找到唯一且匹配的新记录才会补报提交 ID。 |
| 提交表单提示需要额外验证 | 在浏览器里完成提示中的验证。若发生在提交之后，先检查个人提交记录再决定是否重试。 |

## 项目结构与验证

| 路径 | 内容 |
| --- | --- |
| [cf_submit/](cf_submit) | 配置、认证、比赛适配器、网页解析、提交和结果核对 |
| [browser_extension/](browser_extension) | Chrome/Edge 扩展，连接回环端口后使用浏览器内的网页会话 |
| [tests/](tests) | CLI、HTTP、回环通信及扩展测试 |
| [RESEARCH.md](RESEARCH.md) | Codeforces API、网页调用和兼容性预研 |

默认配置目录为 `~/.config/cf-submit/`。浏览器模式只保存配对信息、端口和已确认的账号名，网站凭据保留在浏览器。HTTP 模式另存 Cookie，文件权限为 `600`，配置目录权限为 `700`。

```bash
.venv/bin/python -m unittest discover -v
node tests/test_browser_page.mjs
```

扩展测试使用 Node.js 22+。常规测试使用本地模拟数据，可另行启用实网只读测试：

```bash
CF_SUBMIT_LIVE=1 .venv/bin/python -m unittest -v tests.test_live
```

已验证普通赛浏览器模式的真实提交与提交 ID 核对。Gym 流程由模拟测试覆盖。直连模式是否可用取决于 Codeforces 对当前网络和会话的验证要求。[Codeforces API 文档](https://codeforces.com/apiHelp/methods)
