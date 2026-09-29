# 服务面板

在浏览器里看这台 Linux 机器的资源，并用卡片管理白名单里的 systemd / PM2 服务。只使用 Python 标准库，不需要额外安装依赖。

默认地址是 `http://<主机>:5071`。

## 页面

- **概览**：处理器占用、占用最高的程序、内存、硬盘温度、磁盘占用（含占用最多的三项）、网络流量和网卡速率。
- **服务**：白名单卡片。查看状态、日志和访问链接；启动、停止、重启需要先登录。
- **配置**：登录后可以导入本机已有服务、新建 systemd 单元、修改卡片文字、上传 PNG / SVG 图标、修改密码。

界面有明亮、夜间、护眼三种主题，选择会记在浏览器里。

## 安装

需要 systemd 和 Python 3。面板要能执行 `systemctl`，因此服务以 root 运行。

```bash
sudo mkdir -p /opt/app-cards
sudo cp -a app.py apps.json static /opt/app-cards/
```

写入 `/etc/systemd/system/app-cards.service`：

```ini
[Unit]
Description=Self-built application cards
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/app-cards
ExecStart=/usr/bin/python3 /opt/app-cards/app.py
Restart=on-failure
RestartSec=3
Environment=HOST=0.0.0.0
Environment=PORT=5071
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

然后启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now app-cards.service
```

第一次启动会生成配置密码，写在 `/opt/app-cards/initial-password`。登录配置页后改掉密码，这个文件会被删掉。密码哈希保存在 `auth.json`（权限 `600`）。

也可以不装成服务，直接运行：

```bash
HOST=0.0.0.0 PORT=5071 python3 app.py
```

## 卡片

卡片列表在 `apps.json`。一条 systemd 卡片大致如下：

```json
{
  "id": "example",
  "name": "示例服务",
  "description": "说明",
  "page": "打开后看到的页面",
  "kind": "systemd",
  "unit": "example.service",
  "port": 8080,
  "links": [{ "label": "打开", "href": "http://{host}:8080/" }],
  "note": "",
  "icon": "/static/icons/example.png"
}
```

`{host}` 会换成当前访问面板时用的主机名。PM2 卡片把 `kind` 写成 `pm2`，并用 `pm2` 字段填写进程名。

导入已有服务只是把该单元登记成卡片，并在 `services/` 留下一份单元快照。它不复制程序和数据，也不改写原来的单元文件。从面板移除卡片只删除卡片和快照，不删除 systemd 服务。

新建服务会在 `/etc/systemd/system/` 写入单元文件。服务名只允许字母、数字和连字符，不能覆盖 `ssh`、`sshd`、`app-cards`，也不能使用 `systemd` 开头的名字。

## 目录

| 路径 | 作用 |
|---|---|
| `app.py` | 后端 |
| `static/index.html` | 页面 |
| `apps.json` | 卡片白名单 |
| `static/icons/` | 自定义图标 |
| `auth.json` | 密码哈希，不提交 |
| `initial-password` | 首次密码，不提交 |
| `services/` | 导入时保存的单元快照，不提交 |

## 注意

概览、服务列表和日志不需要登录。启动、停止、重启、导入、新建和改配置需要登录。面板以 root 运行，请只在可信网络里开放 5071 端口。
