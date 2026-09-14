<!-- version: v2.2.0 -->

# 重试反馈模板（版面理解等 JSON 输出任务的重试消息，layout_understand 使用）

本文件按错误类别给出**可执行**的修法：每条错误后面都跟着"怎么做"，不要只把错误原样退回去。

## json_unparseable

assistant：（上一次输出无法解析，见下方错误）

user：

```text
上次输出无法解析，请重新输出**完整** JSON（只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字）：
{errors}
```

## schema_invalid

assistant：（见下方校验错误）

user：

```text
你的输出不符合 quote_schema v1.1，校验错误如下。请修正后重新输出完整 JSON（只输出 JSON）：
{errors}

修正要点：
1. 「None is not of type 'number'」= 该字段不允许 null，必须填数字：回到原文找到该条目金额照抄。
   注意：金额字段 amount_per_pc / amount 的类型是 number|null —— 单据确实没印出金额时 null 是**合法**的，
   不要为了躲开类型错误而填 0 或自己算一个数，那会触发溯源失败。
2. 「is not of type 'array' / 'object'」= 结构写错：六费用模块必须是 {total, items:[...]}；
   tooling 必须是 {total, molds:{total,items}, fixtures:{...}, stencils:{...}} 或 null。
3. 「is a required property」= 字段漏了：items 元素的 name / amount_per_pc / item_type 等必填项都要输出，
   确实没有值也不能省略，按 schema 填 null（金额字段允许 null）。
4. 「is not one of [...]」= 枚举写错：item_type 只能用 损耗/管理费/利润/税费/其他（包装运输为 包装/运输）；
   currency 只能用 CNY/USD/EUR/JPY/HKD/TWD/KRW/OTHER。
5. 除金额字段外，schema 里 string|null 的字段没有值时填 null，不要写 ""、"无"、"未知"之类占位文字。
```

## traceability

assistant：（上一次输出的金额/坐标与单据出处不符，见下方问题清单）

user：

```text
以下金额/坐标与所引用单据出处不符，请逐条核对原文后重新输出完整 JSON（只输出 JSON）：
{errors}

按问题类型逐一修正（括号里是错误中的 issue 名）：

1. amount_not_in_evidence（金额不在你写的 evidence.raw_text 里）—— 三种情况：
   a) 单据根本没印出这个金额（如材料栏只有料重与料价、没有材料费金额）→ amount_per_pc 填 null，
      把印出的要素原文照抄进 note，并列入 _self_check.null_fields；**禁止**相乘/相加把金额算出来。
   b) 借用了相邻单元格的数字 → 回到原文找到该条目自己的金额照抄。
   c) raw_text 写漏了 → raw_text 必须是所引用单元格里逐字照抄的原文（多个单元格用 " / " 分隔），
      金额数字本身必须出现在同一段 raw_text 里。

2. amount_not_in_ir（金额在整个单据里都不存在）
   属于编造值。请从原文重新抄一个真实存在的数字；该条目在原文里没有金额时填 null
   （note 里写明缺失原因），不要自行推算补齐。

3. raw_text_not_verbatim（raw_text 是坐标或 IR 序列化片段）
   raw_text 只能是单元格的文本内容；形如 "8:1|9:28|10:0.96"、"R7C17"、"9:28" 的是坐标流水，不是原文。
   请把对应单元格的文字原样抄进 raw_text。

4. evidence_location_mismatch（raw_text 与 location 指向的单元格内容不一致）
   raw_text 必须就是 location 那片单元格里的文字。常见错误是把表头区的词当成金额出处
   （数据行该列为空却写 raw_text="镭雕"）——表头、标题、栏目名都不是金额出处：数据行该列为空时
   不要建该条目；只有数据行印了数字，才按那一格建条目并照抄金额。

5. invalid_location（坐标超出单据范围 / sheet 名不对）
   location 的 sheet 名要照抄 IR 行首的 sheet 名，行列号必须在单据实际范围内。

6. missing_evidence（条目没写 evidence）
   每个 items 元素都必须有 evidence（file / location / raw_text）。

7. tax_amount_not_in_evidence（税费条目金额无出处）
   单据只有税率、没有印出税额数字时：amount_per_pc 填 null（严禁填 0），rate 照抄税率
   （如"税率 13%"→ rate=0.13），税额由下游脚本按 税率×未税 派生。

通用纪律：只照抄单据上印出来的数字，不做任何跨字段算术推导；查不到的数字一律填 null 并列入
_self_check.null_fields。
```
