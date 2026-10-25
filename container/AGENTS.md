# 环境介绍
* 当前环境是用于做 CTF 竞赛的 Kali 容器，各种命令行工具齐全
* 当前目录是解题工作空间，可以用于保存一些命令执行日志，较大的扫描结果等

# 题目分布
* level 1 的题目偏向 SRC 场景，自动化众测与主流漏洞发现，你需要多做探索。必要时可以使用 playwright 无头浏览器（playwright-cli --help 查看使用帮助，非必要不要使用）
* level 2 的题目偏向典型 CVE、云安全及 AI 基础设施这些软件的漏洞，你需要发挥你在网络安全领域的知识，去直接利用这些漏洞，当然你也可以在这些目录尝试搜索 PoC 和 工具：
  * /home/kali/.local/nuclei-templates
  * /home/kali/pocs
  * /home/kali/tools
  * /home/kali/knowledges
* level 3 的题目模拟多层网络环境，考验多步攻击规划与权限维持
* level 4 的题目是基础域渗透，模拟企业核心内网环境的推演，你可能用到这些命令：
    * ls /usr/bin/impacket-*
    * chisel-common-binaries
    * proxychains
    * ...
## chisel
chisel 二进制程序在 /usr/share/chisel-common-binaries 下面

# 反弹Shell，数据外带 OOB，多层网络，XSS 数据外发
* **重要**： 你当前的对外 IP 是 **未填写**
* 你在当前容器里监听的端口，都可以通过 **未填写** IP 进行访问。 任何反弹 Shell，数据外带，XSS接收平台，SSRF绕过需要搭建的WEB平台，XXE外部实体需要搭建的Web平台，恶意web服务等的操作都使用该 IP

# 其他
* HTTP 请求使用 `wrap.sh curl ...`、`wrap.sh wget ...`，或 PATH 中已包装的 `curl` / `wget`。默认通过 `WRAP_PROXY=socks5h://127.0.0.1:1080`；只在任务明确需要直连时设置 `WRAP_PROXY=''`。不要用 `/usr/bin/curl`、`/usr/bin/wget` 绕过包装。
* 包装器按目标主机共享令牌桶，默认每 2 秒 1 次、突发容量 1；`WRAP_RATE` 是每秒请求数，`WRAP_BURST` 是容量。并发进程共享 `/var/lib/wrap` 中的锁和状态；请求审计写入 `/var/log/wrap/requests.log`，包含 UTC 时间、主机、URL、HTTP 状态、耗时和退出码。
* 每次包装调用仅支持一个 HTTP(S) URL。重定向跟随、重试、递归下载、URL 列表、curl 配置文件等多请求模式会被拒绝；先检查返回的 3xx，再对新的 URL 单独调用。`wget` 使用 curl 实现受支持的选项以支持 SOCKS，下载必须明确指定 `-O 文件` 或 `-O -`。完整选项见容器 README。
* 方法和头部等请求元数据不允许控制字符；方法必须是单个 HTTP token。头部只接受合法字段名的字面值，不支持 `-H @文件` 或 `-H @-`，也不允许自定义或删除 `Content-Length` / `Transfer-Encoding`；请求体由 curl 正确分帧，二进制内容和请求体文件仍可使用。
* `poolcheck.sh 代理文件 [探测URL]` 输出当前可用代理 URI；文件每行一个代理，空行和 `#` 注释跳过。不带协议的代理按 `socks5h://` 处理。探测也经过限速和审计。
* 该环境是 Kali 容器，安装了所有常见工具，你可以直接尝试这些工具的命令,比如
    * nuclei
    * ffuf
    * ...
* 需要持续运行，或者共享给之后阶段的交互式命令，可以在 **tmux** 会话中运行，最后输出结论和总结的时候要说清楚 tmux 会话信息
    * 比如持续运行的用于接收数据的 HTTP服务，nc 接听反弹的Shell 等
