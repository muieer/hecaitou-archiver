# 和菜头文章存档与分析

检查和菜头网站最新发布的文章，在发布时间属于系统当天时保存正文，再调用本机 LM Studio 当前活跃模型，生成 where、when、what、why、how、which、who 七项 JSON 分析。

代码、安装方式、参数和 Agent 调用说明见 [工具使用说明](hecaitou-archiver/README.md)。

## 本地管理页面

在项目根目录启动服务，然后在浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)：

```bash
/usr/bin/python3 -m pip install --user -r hecaitou-archiver/requirements.txt

nohup /usr/bin/python3 hecaitou-archiver/dashboard.py --output-dir './文章存档' > "dashboard.log" 2>&1 < /dev/null & disown
```

页面可设置每天的执行时间（精确到分钟）、开启或关闭调度、立即执行一次，并阅读本地最新文章及七项分析。初始调度关闭，默认待选时间为 `14:30`。关闭浏览器不会停止调度；后台 Python 服务须持续运行，退出服务或电脑休眠期间不执行，错过时间不补跑。

前端使用原生 HTML、CSS、JavaScript，无 Node.js、打包工具或 CDN 依赖。阅读页新增的 Python 依赖为 Markdown 渲染与 HTML 清理库。

## 单次命令行运行

在当前项目根目录运行：

```bash
/usr/bin/python3 hecaitou-archiver/archive.py --output-dir './文章存档'
```

默认 LM Studio 服务地址为 `http://127.0.0.1:2051`，无需身份验证，需要恰好一个已加载的语言模型实例。

运行自动化测试（使用本机临时 HTTP 服务，不调用真实模型）：

```bash
/usr/bin/python3 hecaitou-archiver/selftest.py
```

`文章存档/`、`hecaitou-archiver/test-results/`、Python 虚拟环境和缓存均为本地数据，由 `.gitignore` 排除，首次克隆时不会包含这些目录。自动化测试会生成本地测试摘要和逐项记录。
