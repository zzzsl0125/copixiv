# 事故报告：前端能开、内容全空（后端自 2026-09-12 07:04:55 起死亡）

> 只读排查，未重启、未改动任何服务或配置。所有结论均有下表的命令/日志出处。

## 1. 结论（一句话）

前端 `copixiv-frontend.service`（`vite preview`，:5173）一直活着，但**后端 `copixiv-backend.service` 自 2026-09-12 07:04:55 起处于 inactive (dead)**，
其 `/api` 反向代理目标 `127.0.0.1:9000` 无人监听 → 前端所有 `fetch` 拿到 **502 (ECONNREFUSED)** →
`useCursorPagination` 回落文案「加载失败，请检查网络或后端状态」。**不是网络问题，是后端进程没了。**

## 2. 现场证据

```
$ systemctl status copixiv-backend.service
Active: inactive (dead) since Sat 2026-09-12 07:04:55 CST; 22h ago
Process: 1088 ExecStart=…/.venv/bin/python -u main.py (code=killed, signal=TERM)

$ systemctl show copixiv-backend -p Result -p ExecMainCode -p ExecMainStatus -p NRestarts
Result=success          # 被 systemd 判为「干净退出」
ExecMainCode=2          # CLD_KILLED
ExecMainStatus=15       # SIGTERM
NRestarts=0             # Restart=on-failure 没有触发

$ ss -ltnp | grep -E ':(9000|5433|5173)'
:5173 监听中(前端)      :9000 无      :5433 无(pgserver 也死了)
$ curl -i http://127.0.0.1:5173/api/health
HTTP/1.1 502 Bad Gateway     # vite 代理转发失败
$ .venv/bin/python scripts/pg_dev.py status → pg_ctl: no server running
```

前端侧同一时刻的代理日志（`journalctl -u copixiv-frontend`）：

```
5:17:19 AM [vite] http proxy error: /api/system/config
Error: connect ECONNREFUSED 127.0.0.1:9000
```

代理目标定义在 `frontend/vite.config.ts`（`/api → http://127.0.0.1:9000`），后端端口默认值在
`src/copixiv/app.py:713`（`PORT` 默认 `9000`）。文案出处 `frontend/src/composables/useCursorPagination.ts:66`。

## 3. 时间线（2026-09-12，boot 0）

| 时刻 | 事件 | 出处 |
|---|---|---|
| 07:00:01 | root cron `/sbin/reboot` → 整机重启（每日 07:00 定时重启） | `journalctl` CRON[22617] |
| 07:01:10 | 新 boot 开始 | `journalctl --list-boots` |
| 07:01:31→48 | backend unit 启动，`ExecStartPre=pg_dev.py start` 拉起 pgserver(5433) | journal |
| 07:02:34→46 | app 初始化：pixiv patch / Alembic / 5 个账号 | `log/backend.log` |
| 07:02:50 | 5 个 pixiv 账号认证全部失败：`127.0.0.1:20172` 代理尚未就绪（v2rayA 07:03:29 才起） | journal |
| 07:03:28→07:04:44 | frontend unit：`npm run build` 完成并启动 `vite preview` | journal |
| 07:04:44 | `Reached target multi-user/graphical`，`Startup finished … = 4min 15.580s` | journal |
| 07:04:53 | 数据库预热完成 | `log/backend.log` |
| 07:04:54 | 3 个 cron 任务注册、`TaskManagerSystem started` —— **应用刚刚就绪** | `log/backend.log` |
| 07:04:54 | `Shutting down copixiv v2...` + `CronScheduler stopped.`（收到 SIGTERM，优雅退出） | `log/backend.log` |
| 07:04:54.242 | pgserver 收到 smart shutdown（随 unit cgroup 一起被 TERM） | `.spike/pg.log` |
| 07:04:55.90 | systemd：`copixiv-backend.service: Deactivated successfully.` | journal |

**复发史**：Sep 10 07:04:48 出现过完全相同的「刚就绪即静默退出」（无 `Stopping`、无重启），
当天 14:35:58 由人手工 `sudo systemctl restart copixiv-backend.service copixiv-frontend.service` 救回；
Sep 8 / 9 / 11 三次 boot 均正常存活。最后一次 API 访问是 Sep 11 23:36（`log/access.log`），
即：**Sep 12 早上重启后应用就没再起来过，故障已持续约 22 小时。**

## 4. 为什么死了不自动拉起（这是真正需要修的）

1. systemd 对 `Type=simple` 服务把 **SIGHUP/SIGINT/SIGTERM/SIGPIPE 视为 clean exit**，
   而 **`Restart=on-failure` 明确排除这四个信号**（`man systemd.service`：*"is terminated by a signal
   (…excluding the aforementioned four signals)…"*）。于是 `Result=success`、`NRestarts=0`，永不重启。
2. PostgreSQL 是 backend unit 的 `ExecStartPre` 拉起的、**与 app 同在一个 cgroup**：
   app 一死，systemd 顺手把 DB 一起 TERM 掉，且没有任何 unit 再把 DB 拉回来。
3. `deploy/copixiv-backend.service` 把 `StartLimitIntervalSec` 写在 `[Service]`（systemd 忽略并告警，
   backend:40 / frontend:47）——限流配置实际未生效。

## 5. SIGTERM 是谁发的：已排除的与未定的

- **不是 systemd stop**：systemd 主动停会打 `Stopping copixiv-backend.service …`（Sep 9 00:02、Sep 12 07:00 都有），这两次静默死亡都没有。
- **不是特权操作**：Sep 12 整个 boot **没有任何 `sudo[…]` 记录**（sudo 命令会被 journal 记录；Sep 10 14:35 那次人工恢复就留下了）。→ 是**同 uid(invocation) 的普通 kill/pkill**。
- **不是 cron**：07:00–07:10 只有两条 cron（07:00:01 `/sbin/reboot`、07:05:01 `debian-sa1`）。
- **不是 DSH/agent 会话**：`~/.dsh/sessions/**` 在 Sep 12 06:00–12:00 无任何写入（会话记录是实时落盘的，已用当前会话反证）。
- **不是 OOM**：无内核 OOM 记录，且 OOM 是 SIGKILL 不是 TERM。
- 当时只有控制器主机 192.168.196.2 的两条 SSH 会话（07:03:42 开到 22:13:22）；**在其中一个 shell 里手敲 `kill`/`pkill` 不会留下任何日志**。
- 结论：**信号来源无法从现有遥测（无 auditd、无进程记账）回溯**。这与「Sep 10 也发生过一次」一致——像是某个 boot 末期脚本/人手动清理，而不是周期任务。

## 6. 恢复（需你在自己的终端执行；我的沙箱内 `sudo` 被 no_new_privileges 拦住）

```bash
sudo systemctl start copixiv-backend        # ExecStartPre 会先拉起 pgserver(5433)
systemctl is-active copixiv-backend
curl -s -o /dev/null -w 'backend=%{http_code}\n' http://127.0.0.1:9000/api/system/config
curl -s -o /dev/null -w 'via-frontend=%{http_code}\n' http://127.0.0.1:5173/api/system/config
```

两个 `200` 后刷新页面即可恢复（前端无需重启、dist 无需重建）。
重启前的环境已确认健康：9000 空闲、磁盘 57%、内存可用 5.2G、PG 数据目录是干净关闭、pixiv 代理 20172 现在已监听。

## 7. 建议加固（未执行，等你决定）

1. `deploy/copixiv-backend.service`（及 `/etc/systemd/system/` 同名文件）：`Restart=on-failure` → **`Restart=always`**
   （本次故障本可在 5 秒内自愈）；如坚持 on-failure，则需加 `RestartForceExitStatus=SIGTERM`。
2. 把两个 unit 的 `StartLimitIntervalSec/StartLimitBurst` 从 `[Service]` **挪到 `[Unit]`**。
3. 可选：给 PostgreSQL 单独一个 unit（现在它与 app 同 cgroup，停 app 即停库）。
4. 可选：抓下一次信号来源——装 auditd 后
   `auditctl -a always,exit -F arch=b64 -S kill,tgkill -F a1=15 -k term-sender`（audit 会记录发送方 pid/exe/uid）。
5. 可选：主机上已有 monit 在跑，可加一条对 `http://127.0.0.1:9000/api/system/config` 的进程/端口检查做告警。

## 8. 风险提示

主机每天 **07:00 由 root cron 定时重启**，应用在约 07:04:50 完成启动——
也就是说同样的事故窗口**每个早上都会重演一次**；在加固前，一旦再被 TERM，会一直躺到有人手工 `systemctl start`。
