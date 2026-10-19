# cfopt

**按落地机房优选 Cloudflare 边缘 IP，并自动下发最优节点。**

解决一个具体问题：域名挂在 Cloudflare 后面（Tunnel / 普通代理），国内访问总被调度到 SJC、LAX 这些远端机房，延迟 180ms+。而 CF 明明有香港节点，只是没被选中。

本工具扫出所有**真正落在目标机房**的 IP，然后自动把最快的几个写进 `/etc/hosts`，并按周期持续维护。

## 为什么需要它

| | CloudflareSpeedTest | cfopt |
|---|---|---|
| 测延迟 | ✅ | ✅ |
| 测下载速度 | ✅ | ✅ |
| **识别落地机房** | ❌ 不显示 | ✅ 解析 `CF-RAY` |
| **按机房筛选** | ❌ | ✅ 只要 HKG |
| 测私有服务（403/522） | ⚠️ 会失败 | ✅ `CF-RAY` 头仍在 |
| 自动下发 | ❌ 手动改 | ✅ 写 hosts / DNS |
| 多域名管理 | ❌ | ✅ Web 面板 |

关键差异：CFST 给你一堆「80ms」的 IP，但你不知道哪个在香港。本工具直连 TLS 时读响应头里的 `CF-RAY`（形如 `a4ca4e02af43ec2b-HKG`，末段即机房），直接判定。

**即使服务返回 403/404/522，这个头依然存在**，所以能测 Access 保护的私有服务。

## 实测效果

测试环境：国内宽带 → Cloudflare Tunnel（回源到家用 NAS）

| 指标 | 默认解析（SJC） | 优选后（HKG） | 变化 |
|---|---|---|---|
| TCP 握手 | 187 ms | 60 ms | **-68%** |
| TTFB | 924 ms | 619 ms | -33% |
| 下载速度 | 1.35 MB/s | 5.29 MB/s | **+292%** |
| 全量扫描 | — | 248 s（11912 个候选） | 首轮之后约 30 s |

机房分布（5956 个 IP 抽样）：

```
LAX    1465 个
SJC     369 个
HKG     316 个   <-- 目标
SEA     170 个
LHR     122 个
```

## 两种部署方式

| | fpk（推荐，飞牛OS） | Docker |
|---|---|---|
| 安装方式 | 应用中心手动安装 | `docker compose up -d` |
| 依赖 | 只需 python312 运行时 | 需要 Docker |
| 网络要求 | 无需联网 | 需要能拉基础镜像 |
| 适合 | 飞牛OS 用户 | 任何 Linux |

### 方式一：飞牛 fpk（免 Docker）

```bash
# 从 Releases 下载 cfopt.fpk，然后在飞牛OS：
#   应用中心 → 手动安装 → 选择 cfopt.fpk
# 或命令行：
appcenter-cli install /path/to/cfopt.fpk
```

装完桌面出现「Cloudflare 优选」图标，点开即管理面板。

详见 [fpk/README-fpk.md](fpk/README-fpk.md)。

### 方式二：Docker

```bash
git clone https://github.com/kaynai1/cfopt.git
cd cfopt

# 本机模式（只改这台机器的 hosts）
docker compose -f docker-compose.single.yml up -d --build

# 或 DNS 模式（给其他设备提供解析）
docker compose up -d --build
```

打开 `http://你的NAS:8080/` 添加域名。

## 使用

不管哪种部署，流程都一样：

1. 打开管理面板（`http://NAS的IP:8080/`）
2. 填域名（必须已接入 Cloudflare 且开了小黄云）
3. 点「添加并扫描」
4. 等首轮扫描（约 4 分钟，页面显示实时进度）
5. 自动写入 hosts，之后每 6 小时自动维护

管理面板显示：

- **接入状态**：已接入 / 扫描中（带百分比）/ 未接入
- **当前下发**：IP + 机房 + 延迟
- **候选节点表**：全部命中节点按延迟排序，带对比条和「使用中」标记
- **机房分布**：HKG 316 / LAX 1465 等，命中机房高亮

## 配置

| 变量 | 说明 | 默认 |
|---|---|---|
| `TARGET_HOST` | 初始域名（可留空，在面板添加） | 空 |
| `COLOS` | 目标机房，逗号分隔 | `HKG` |
| `MODE` | `hosts` 只改本机 / `dns` 提供解析 | `hosts` |
| `SYSTEM_HOSTS` | hosts 模式写入的文件 | `/etc/hosts` |
| `WORKERS` | 并发数 | `200` |
| `TIMEOUT` | 单 IP 超时（秒） | `3` |
| `INTERVAL` | 扫描周期（秒） | `21600` |
| `TOP_N` | 下发节点数 | `2` |
| `HTTP_PORT` | 面板端口 | `8080` |
| `DATA_DIR` | 数据目录 | `/data` |

常用机房：`HKG` 香港、`NRT` 东京、`SIN` 新加坡、`LAX` 洛杉矶、`SJC` 圣何塞。

## REST API

```bash
# 列出域名（含节点明细和进度）
curl -s http://localhost:8080/api/domains | jq

# 添加域名（自动触发扫描）
curl -X POST http://localhost:8080/api/domains \
  -H "Content-Type: application/json" \
  -d '{"host":"ai2.kdns.fr","colos":["HKG"],"top_n":2}'

# 立即重扫
curl -X POST http://localhost:8080/api/domains/<id>/scan

# 删除
curl -X DELETE http://localhost:8080/api/domains/<id>

# 健康检查（compose healthcheck 用）
curl -s http://localhost:8080/health
```

## 命令行

```bash
cfopt scan -H ai2.kdns.fr -c HKG -n 8       # 扫描
cfopt nets --from-json scan.json -c HKG      # 提取网段
cfopt check -H ai2.kdns.fr --ips 1.2.3.4     # 查单个 IP 的机房
cfopt bench -H ai2.kdns.fr --ips 1.2.3.4     # 下载测速
cfopt verify -H ai2.kdns.fr -p 5353          # 验证 DNS 生效
cfopt serve                                   # 启动服务
```

## 性能说明

扫描速度取决于两个参数，这是实测调优的结果：

| 参数组合 | 单个死 IP 耗时 | 全量 11912 个 |
|---|---|---|
| timeout=4 + 重试 2 次 | 12.5 s | 约 13 分钟 |
| **timeout=3 + 不重试**（当前默认） | 3.0 s | **248 秒** |

重试对死 IP 是纯浪费（3 次都是超时）。为了不牺牲准确性，只对延迟最好的前 20 个复测一次，取较慢值，避免抖动快的节点被误选。

**首轮全量扫描后**会缓存 200 个目标机房网段，之后每轮只扫这些段，约 30 秒。

## 安全设计

核心原则：**任何情况下不能让域名解析挂掉**。

| 场景 | 行为 |
|---|---|
| 有缓存 | 直接用 |
| 无缓存 | DNS 先转发上游；扫描完自动切换 |
| 扫描失败 | 保留上次结果 |
| 从未扫出结果 | 回退上游，站点照常可访问 |
| 空 IP 列表 | **回退上游**，绝不返回空应答 |

最后一条是踩过的坑：早期版本无结果时返回 `ANCOUNT=0`，域名直接解析不了，站点打不开。现在有回归测试守着。

## 已知限制

**只影响本机**。写的是 `/etc/hosts`，其他设备访问同一域名时，走的仍是 Cloudflare 的 Anycast 调度，不会因为这台机器改了 hosts 而变快。

**优化不了回源段**。访问者 → CF 边缘这段能优化；CF 边缘 → argotunnel → 你的 NAS 这段由 cloudflared 决定，跟解析哪个 IP 无关。实测这段有约 500ms 固定开销。

**测速网站看不到效果**。像 tcptest.cn 这类网站从公网发起请求，用公网 DNS 解析，拿到的永远是 CF 的原生 Anycast IP，跟你本机 hosts 无关。这是预期行为，不是 bug。

**扫描频率别太短**。高频 TLS 握手会被 CF 判定为探测行为。建议 ≥1 小时。

## 开发

```bash
git clone https://github.com/kaynai1/cfopt.git
cd cfopt

# 跑测试（72 项，不依赖外网）
PYTHONPATH=src python -m unittest discover -s tests -v

# 本地起服务
TARGET_HOST=ai2.kdns.fr DNS_LISTEN=127.0.0.1:5353 \
  HTTP_PORT=8080 DATA_DIR=./data PYTHONPATH=src python -m cfopt.main
```

代码结构：

```
src/cfopt/
├── probe.py     CF-RAY 解析、延迟测量、网段展开、CF 归属校验
├── dnssrv.py    DNS 服务：报文解析、A 记录构造、上游转发
├── store.py     多域名配置持久化
├── scanner.py   多域名扫描调度
├── web.py       Web 管理界面 + REST API
└── main.py      守护进程入口
fpk/            飞牛应用包（源码由 build-fpk.sh 从 src/ 同步）
```

## CI/CD

推送到 `main` 自动跑：lint → 3 OS × 3 Python 测试矩阵 → wheel → PyInstaller 4 平台 → 多架构 Docker 镜像推 GHCR。

打 tag（`v2.1.0`）自动创建 Release。

## License

MIT
