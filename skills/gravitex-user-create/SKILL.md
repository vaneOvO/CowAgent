---
name: gravitex-user-create
description: 在 Gravitex 后台管理系统创建新用户账号。当用户要求创建 Gravitex 账号、开通 API 账户、或提到 Gravitex 用户管理时使用。
metadata:
  cowagent:
    default_enabled: true
    emoji: "👤"
---

# Gravitex 用户创建

## ⚠️ 重要规则

1. **禁止输出任何中间文字**，不要汇报进度。
2. **只在最后调用一次 send 工具**返回最终结果。
3. **必须使用脚本完成操作**，不要用 browser 工具逐步操作。如果脚本失败，通过 send 工具把错误信息发给用户，不要回退到 browser 工具手动操作。

## 执行流程（2轮完成）

### 第1轮：运行自动化脚本

使用 bash 工具执行：

```bash
python3 /home/agent/cow/skills/gravitex/create_user.py {用户指定的金额数字}
```

脚本会自动完成登录、导航、填表、提交，并以 JSON 格式返回结果：
```json
{"success": true, "username": "user202605161242", "amount": "10", "page_result": "系统返回的文本内容"}
```

从 page_result 中提取系统返回的账号和密码。

### 第2轮：发送结果

脚本返回 JSON 格式：
```json
{"success": true, "username": "user202605161335", "password": "JQ14xg2wDg68", "amount": "10", "page_result": "操作成功"}
```

直接从 JSON 的 `username` 和 `password` 字段取值，通过 send 工具发送：

```
✅ 用户创建成功

平台地址：https://maas.gravitex.ai
API接口文档：https://docs.gravitex.ai/zh
账号：{JSON中username的值}
密码：{JSON中password的值}
初始余额：${JSON中amount的值} USD
```

## 注意事项

- 账号和密码直接从脚本返回的 JSON 字段获取，不要自己生成
- 如果脚本返回 `"success": false`，通过 send 告知用户错误信息
- **不要输出任何中间文字，不要发送中间消息**
