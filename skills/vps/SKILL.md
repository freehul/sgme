---
name: vps
description: VPS(hk) 加固变更与登录方式：fail2ban / SSH / vnstat 变更记录与登录要点。
tags:
  - skill
category: vps
---

# VPS 加固变更记录（2026-08-18）

## 背景
排查 VPS（43.255.156.6）流量暴增：确认无盗用，连接 100% 来自用户本人 IP；陌生 IP 均为未通过 Reality 认证的端口扫描，零实际流量。顺带发现 SSH 密码登录实际生效（被 50-cloud-init.conf 覆盖）、8458 次爆破记录。

## 变更清单（已执行并验证）
1. **SSH 登录用户 root → leo**
   - 新增系统用户 leo（uid=1000），加入 sudo 组，配置 NOPASSWD 免密 sudo
   - 公钥沿用原 root 的 authorized_keys（密钥文件 ~/.ssh/vps43_ed25519 不变）
   - 本地 ~/.ssh/config 的 vps 别名 User 已改为 leo
   - **连接方式：ssh vps（= leo@43.255.156.6:2222，密钥 vps43_ed25519）**
   - root 直连已被拒（Permission denied publickey），勿再用 root@43.255.156.6
2. **禁用密码登录**
   - /etc/ssh/sshd_config.d/50-cloud-init.conf 内容改为 PasswordAuthentication no
   - 生效验证：sshd -T 显示 passwordauthentication no
3. **禁用 root 直登**
   - /etc/ssh/sshd_config 中 PermitRootLogin yes → no
4. **vnstat 按天流量统计**（新装）
   - 服务 active，接口 ens18 已入库；查看：sudo vnstat -d
5. **fail2ban SSH 防护**（新装）
   - jail 配置 /etc/fail2ban/jail.d/sshd-local.conf：port 2222、maxretry 5、findtime 10m、bantime 1h
   - 已封禁爆破 IP 195.178.110.220
   - 查看：sudo fail2ban-client status sshd

## 备份与回滚
- /etc/ssh/sshd_config.bak.20260818
- /etc/ssh/sshd_config.d/50-cloud-init.conf.bak.20260818

## 注意
- leo 的 sudo 为 NOPASSWD:ALL：拿到 leo 密钥 = 拿到 root，密钥须严格保密
- vnstat 从 2026-08-18 起积累数据，流量排查先看 vnstat -d 再对 xray 日志
