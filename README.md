# 微博监控插件 v3

AstrBot 微博监控插件，定时爬取指定微博用户的新动态并推送到指定群聊或私聊。

## 功能特性

- 定时监控指定微博用户的新动态
- 支持跳过置顶微博
- **自动爬取并推送图片（最多一张）**
- **支持转发动态爬取**
- **支持推送到群聊和私聊**
- 多API自动切换（移动版/网页版）
- 手动触发检查指令
- 状态查询

## 安装

1. 下载插件ZIP并解压到 AstrBot 插件目录
2. 安装依赖：`pip install -r requirements.txt`
3. **安装 Playwright 浏览器**（Docker 环境）：
   ```bash
   playwright install chromium
   ```
4. 在 AstrBot WebUI 中配置插件参数

## 依赖要求

- **Playwright Chromium**：用于自动登录
  - 安装命令：`playwright install chromium`
- Python 依赖：见 `requirements.txt`

## 配置说明

### cookies（必需）

微博登录 Cookie，用于获取用户动态。

获取方法：
1. 在浏览器中登录 [微博](https://weibo.com)
2. 按 F12 打开开发者工具
3. 切换到 Network（网络）标签
4. 刷新页面，找到任意请求
5. 在请求头中找到 Cookie，复制完整值

### watch_users

要监控的微博用户 ID，每行一个。

获取用户 ID：
- 打开目标用户的微博主页
- URL 中的数字就是用户 ID
- 例如：`https://weibo.com/u/1195230310` → ID 为 `1195230310`

### target_groups

接收推送的群聊 ID，每行一个。

### target_users

接收推送的私聊用户 ID，每行一个。

### include_retweet

是否包含转发动态，默认开启。

### check_interval

检查间隔，单位为分钟，默认 10 分钟。

### skip_top_post

是否跳过置顶微博，默认开启。

## 指令列表

| 指令 | 说明 |
|------|------|
| `/微博监控` | 显示帮助信息 |
| `/微博状态` | 查看当前监控状态 |
| `/微博测试` | 测试微博连接 |
| `/微博推送` | 手动触发一次检查 |
| `/微博登录` | 自动登录微博获取Cookie（需要Chrome浏览器和selenium） |
| `/微博刷新Cookie` | 手动刷新Cookie |

## 推送消息格式

**普通微博：**
```
🔔 用户名 更新微博啦！
━━━━━━━━━━━━━━━━
微博内容...

🔗 https://weibo.com/...
━━━━━━━━━━━━━━━━
```

**转发动态：**
```
🔄 用户名 转发了一条微博！
━━━━━━━━━━━━━━━━
📝 转发言论:
转发内容...

━━━━━━━━━━━━━━━━
🔁 原文来自 @原作者:
原文内容...

🔗 原文链接
━━━━━━━━━━━━━━━━
```

## 注意事项

- Cookie 有时效性，如推送失败请更新 Cookie
- 建议使用小号登录微博获取 Cookie
- 图片会自动压缩以适应平台限制
- 同一用户的新动态只会推送一次

## 自动登录

插件支持自动登录微博获取 Cookie，使用命令 `/微博登录` 即可。

**要求：**
- 已安装 playwright：`pip install playwright`
- 已安装浏览器：`playwright install chromium`
