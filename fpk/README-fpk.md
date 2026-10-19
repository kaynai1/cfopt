# 飞牛 fpk 安装说明

## 产物

`cfopt.fpk`（35.4 KB）—— 用飞牛官方 `fnpack 1.2.3` 打包。

## 为什么不用 Docker 了

上一版是 Docker 方案，安装时会去拉 `python:3.12-slim`。你的环境里
`registry-1.docker.io` 不可达，导致：

```
Error response from daemon: Get "https://registry-1.docker.io/v2/":
net/http: request canceled while waiting for connection
```

**这一版改成原生 Python 运行**：代码是纯标准库（零第三方依赖），
manifest 里声明 `install_dep_apps=python312`，飞牛会自动装好 Python 运行时。

结果：**不需要 Docker，不需要拉任何镜像，安装即用。**

## 安装

1. 应用中心 → 手动安装 → 选 `cfopt.fpk`
2. 或命令行：`appcenter-cli install /path/to/cfopt.fpk`

安装过程只做两件事：建数据目录、检查 Python 运行时。秒级完成。

## 使用

装好后飞牛OS 桌面出现「Cloudflare 优选」图标，点开即管理面板。
也可直接访问 `http://飞牛OS的IP:8080/`。

流程：填域名 → 点「添加并扫描」→ 自动优选 → 自动写入 `/etc/hosts`。

## 目录说明

| 路径 | 用途 |
|---|---|
| `/var/apps/cfopt/var` | 域名配置、扫描结果、运行日志 `cfopt.log` |
| `/etc/hosts` | 优选结果写入这里（标记区块 `cfopt-managed`） |

卸载时自动清理 hosts 区块，并备份到 `/etc/hosts.cfopt.bak`。

## 排查

服务起不来时看日志：

```bash
tail -50 /var/apps/cfopt/var/cfopt.log
```

手动重启：

```bash
/var/apps/cfopt/cmd/main stop
/var/apps/cfopt/cmd/main start
/var/apps/cfopt/cmd/main status   # 0=运行中 3=未运行
```

## 权限说明

`config/privilege` 设为 `run-as: root`。这是必需的：写入 `/etc/hosts`
需要 root 权限。官方文档建议尽量用 package 用户，但本应用的**核心功能**
就是改 hosts，没有更窄的方案。

## 重新打包

```bash
# 1. 同步最新源码
cp -r ../../docker/cfopt/src/cfopt app/src/

# 2. 打包
fnpack build

# 3. 产物
ls -lh cfopt.fpk
```

## 已知限制

- **只影响飞牛OS 本机**：写的是宿主机 `/etc/hosts`，其他设备不受影响
- 需要 `python312` 运行时（manifest 已声明，飞牛自动安装）
- 域名必须已托管到 Cloudflare 且开启小黄云，否则添加时会被拒绝
