<!-- version: v2.1.0 -->

# 重试反馈模板（版面理解等 JSON 输出任务的重试消息，layout_understand 使用）

## json_unparseable

assistant：（上一次输出无法解析，见下方错误）

user：

```text
上次输出有误，请修正后重新输出完整 JSON：
{errors}
```

## schema_invalid

assistant：（见下方校验错误）

user：

```text
你的输出不符合 quote_schema v1.1，校验错误如下。请修正后重新输出完整 JSON（只输出 JSON）：
{errors}
```

## traceability

assistant：（上一次输出的金额/坐标与单据出处不符，见下方问题清单）

user：

```text
以下金额/坐标与所引用单据出处不符，请逐条核对原文后重新输出完整 JSON
（金额必须能在对应 evidence.raw_text 中找到；找不到的填 null 并列入 _self_check.null_fields）：
{errors}
```
