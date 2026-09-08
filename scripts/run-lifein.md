# 常驻运行(Windows)

`run-lifein.cmd` 让服务开机自启、崩了自动重启。**脚本本身是纯 ASCII 的**,
理由和所有设计一起写在这里。

## 为什么脚本里不写中文注释

cmd.exe 读批处理时,即使 `chcp 65001` 也会在多字节字符上丢同步 ——
**症状是某一行 `rem` 注释被当成命令执行**。踩过一次,查了十分钟。
所以脚本里只有英文,解释放在这份文档。

## 脚本做的四件事

**① 单实例守卫,而且在碰日志文件之前**

两个进程会抢微信的长轮询会话,表现是"消息一会儿到一会儿不到" ——
而开机自启和手动启动很容易撞上。

守卫必须在写日志之前:运行中的进程独占着日志文件,第二个实例一写日志就
失败退出,**那种退出是无声的**,你只会看到"服务好像没起来"。

**② 等数据库最多 5 分钟**

开机时 Docker Desktop 往往比登录任务慢一两分钟。不等的话第一次连接失败,
进程退出,虽然会重启但白白多绕一圈。

**③ 日志追加到 `logs\lifein.log`**

这个系统最危险的失效方式是**安静**([R8](../docs/05-risks.md) / R9)。
没有日志的话,连"它什么时候停的"都答不出来。

`PYTHONUTF8=1` 和 `PYTHONIOENCODING=utf-8` 不能省:重定向到文件时 Python
默认用控制台代码页,中文日志会变成乱码。

**④ 崩了 30 秒后自动重启**

自托管没有值班的人。

## 装/卸开机自启

启动项是一个 VBS,用它包一层是为了**隐藏黑窗口**:

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\LifeIn.vbs
```

内容:

```vbs
CreateObject("WScript.Shell").Run "<仓库路径>\scriptsun-lifein.cmd", 0, False
```

不用任务计划程序是因为 `schtasks /Create` 在这台机器上要管理员权限,
而启动文件夹不需要 —— 效果一样。

**卸载**:删掉那个 `LifeIn.vbs` 即可。

## 数据库也要能自己活过来

```
docker update --restart unless-stopped lifein-pg
```

另外记得在 Docker Desktop 的设置里勾上开机自启,否则数据库不会起来,
脚本会等满 5 分钟然后反复重试。

## 查状态

```powershell
# 服务在不在
Test-NetConnection 127.0.0.1 -Port 8000

# 最近的日志（跳过长轮询的噪音）
Get-Content logs\lifein.log -Tail 40 | Select-String -NotMatch getupdates

# 今天的任务跑了没
docker exec lifein-pg psql -U lifein -d lifein -c "SELECT window_start, status, stats FROM job_runs ORDER BY started_at DESC LIMIT 5"
```
