# P0 技术预研

验证日期为 2026-11-22（北京时间）。

## cf-tool 的实现

1. `client/login.go` 新建 Cookie jar，GET `/enter`，从页面脚本读 CSRF，提交账号和密码，用返回页面的 JavaScript `handle` 判断是否登录。`client/client.go` 将 Cookie、可解密密码和指纹字段保存在一个文件中，并在下次启动时恢复 jar。新工具可沿用 HTTP Session、先 GET 再 POST、验证账户身份的流程，分离 Cookie 存储且只保留会话。[登录源码](https://github.com/xalanq/cf-tool/blob/master/client/login.go) · [会话源码](https://github.com/xalanq/cf-tool/blob/master/client/client.go)
2. `client/info.go` 普通赛使用 `/contest/{id}`，Gym 使用 `/gym/{id}`。两类页面均有 `/problem/{index}`、`/submit`、`/my`、`/submission/{submissionId}`。题目导航及结果链接可沿用这些路由，表单 POST 目标须读实际 HTML。[路由源码](https://github.com/xalanq/cf-tool/blob/master/client/info.go) · [普通赛主页](https://codeforces.com/contest/4) · [Gym 主页](https://codeforces.com/gym/104172)
3. `client/submit.go` GET 提交页，检查账户、取 CSRF，将 token 附到 URL 和表单，POST 源码。历史字段包括 `action`、`submittedProblemIndex`、`programTypeId`、`contestId`、`source`、`tabSize`、`sourceCodeConfirmed`、`ftaa`、`bfaa` 和 `_tta`。旧代码随机生成 `ftaa`，硬编码 `bfaa`、`_tta` 和多项表单默认值。新实现以页面的 POST 表单、可用控件和已有值为准，遇到需要客户端验证的页面停止。[提交源码](https://github.com/xalanq/cf-tool/blob/master/client/submit.go)
4. `client/langs.go` 将语言下拉选项导出为静态表，内含 G++17 等旧选项。新实现只从本次提交表单的选项值获取 ID。当前公开状态列表可见 `C++23 (GCC 14-64, msys2)`，Codeforces 的公告也记载过 `GNU G++23 14.2 (64 bit, msys2)`。可精确匹配这两个 GNU C++23 命名形式，遇到多个有效 ID 须报错。[语言源码](https://github.com/xalanq/cf-tool/blob/master/client/langs.go) · [官方编译器公告](https://codeforces.com/topic/133672/en8) · [Gym 状态页](https://codeforces.com/gym/104172/status)
5. 旧工具用页面报错或成功提示判断响应，成功后读取 `/my` 第一行（`data-submission-id`、`.id-cell`）并等待评测结束。`cmd/submit.go` 会在登录失效后再次调用提交。新实现优先使用响应指向的详情链接，并用提交前后的个人记录及问题和语言辅助确认 ID。只发送一次提交 POST。[记录解析源码](https://github.com/xalanq/cf-tool/blob/master/client/watch.go) · [命令源码](https://github.com/xalanq/cf-tool/blob/master/cmd/submit.go)

## 最初的直连只读验证

| 检查 | 观察结果 |
| --- | --- |
| [普通赛 4 API](https://codeforces.com/api/contest.standings?contestId=4) | HTTPS 200，返回 A–D 的题号、名称及比赛名。官方文档要求公开普通赛只传 `contestId`。 |
| [Gym 104172 API](https://codeforces.com/api/contest.standings?contestId=104172) | HTTPS 400，返回 `You have to be authenticated to use this method`。Gym 改用网页 Session 读取题表。 |
| [Gym 104172 主页](https://codeforces.com/gym/104172) | 网页索引展示 A–L 的题号名称及 Submit Code、My Submissions 导航。当前 Ubuntu 的实际页面请求返回 Cloudflare 403。 |
| [普通赛 4 公开状态 API](https://codeforces.com/api/contest.status?contestId=4&from=1&count=1) 和 [Gym 104172 公开状态页](https://codeforces.com/gym/104172/status) | 公开历史记录有真实数字 ID，包含目前使用的 C++23 语言名称。详情访问尚未证实。 |
| `/enter`、`/contest/4/submit`、`/gym/104172/submit` | 本环境的直接 HTTPS GET 返回 403，页面标题为 `Just a moment...` 并含 Cloudflare challenge。没有观察到已登录表单、当前动态字段或语言 ID。 |

这些是实现浏览器模式之前的直连观测。当前实现用独立的回环中继连接用户浏览器，网页登录及 Cloudflare 验证在浏览器中手动完成。

## 浏览器模式验证

- 已观察到普通赛浏览器模式的真实提交，并用官方只读 API 验证返回的提交 ID。回执不完整时，工具比较发出请求前后的记录，只采用账号、比赛、题号、语言和时间都匹配的唯一新记录。
- Gym 的题表解析、表单预检查和提交回执由模拟测试覆盖。有实际比赛权限时可以用 `submit --dry-run` 核对当前网页结构。
- 单元和模拟测试使用临时缓存和假账号。可选实网测试只发送 GET。

[Codeforces 官方 API 方法文档](https://codeforces.com/apiHelp/methods#contest.standings)
