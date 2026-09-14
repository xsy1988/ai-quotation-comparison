<!-- version: v1.2.0 -->

# 负例 1：惠州市创锋科技有限公司报价单（税率列只有费率、没有税额）

单据同一行相邻单元格原文：
- "不良率 20% 2.01" —— 2.01 是损耗科目的金额，归"不良率损耗"（item_type=损耗，rate=0.2）
- "税率 13%" —— 只有费率，行里没有税额数字

❌ 错误示范：税费条目把金额抄成相邻的 2.01

```json
{"name": "税费", "amount_per_pc": 2.01, "item_type": "税费", "rate": 0.13,
 "evidence": {"file": "报价单", "location": "page_1!R4C22",
              "raw_text": "税率 13%"}}
```

问题：evidence.raw_text 只有"税率 13%"，并不含 2.01 —— 金额借用相邻单元格，违反数字溯源宪法。

❌ 同样错误：把 amount_per_pc 填 0 —— 单据没有印税额就是缺失，0 也是没有出处的编造值
（业务上不存在税额为 0 的报价），填 0 会让下游把税额当成 0 而不是按税率派生。

✅ 正确做法：税费条目只有费率、没有金额 → amount_per_pc 填 null、rate 填 0.13，并把该单元格列入自检：

```json
{"name": "税费", "amount_per_pc": null, "item_type": "税费", "rate": 0.13,
 "evidence": {"file": "报价单", "location": "page_1!R4C22",
              "raw_text": "税率 13%"}}
```

```json
"_self_check": {
  "amounts_traceable": true,
  "no_invented_values": true,
  "tax_amount_null_if_unprinted": true,
  "null_fields": ["unit_price.sga_tax.items[3].amount_per_pc"],
  "uncertain_cells": [{"location": "page_1!R4C22",
                       "reason": "税率13%只有费率，相邻的 2.01 属损耗科目，税额缺失填 null"}]
}
```

---

# 负例 2：材料栏只印出料重与料价、整行没有材料费金额（真实事故）

单据同一行原文（材料栏只有两个要素，没有"材料费"列）：
- "8:1|9:28|10:0.96" 的原始含义是：第 8 列=用量 1，第 9 列=料重 28（g），第 10 列=料价 0.96（元/kg）
- 整行**没有印出材料费金额**（材料费=料重×料价 是下游脚本的事）

❌ 错误示范：自己做乘法算材料费

```json
{"name": "原材料", "amount_per_pc": 26.88, "spec": null, "note": null,
 "evidence": {"file": "报价单", "location": "报价单!R3C9:R3C10",
              "raw_text": "8:1|9:28|10:0.96"}}
```

问题有三处，全部违规：
1. 26.88 = 28 × 0.96 是自己算出来的数（跨字段算术推导），单据上没有这个数字；
2. evidence.raw_text 写成了 IR 序列化片段 "8:1|9:28|10:0.96" —— 这是坐标流水，不是单元格原文；
3. 金额 26.88 不可能出现在任何单元格里，溯源必然失败。

✅ 正确做法：只照抄要素，金额留 null

```json
{"name": "铝合金材料", "amount_per_pc": null, "spec": null,
 "note": "单据只印出料重 28、料价 0.96，未印出材料费金额",
 "evidence": {"file": "报价单", "location": "报价单!R3C9:R3C10",
              "raw_text": "28 / 0.96"}}
```

```json
"_self_check": {
  "amounts_traceable": true,
  "no_invented_values": true,
  "null_fields": ["unit_price.materials.items[0].amount_per_pc"],
  "uncertain_cells": [{"location": "报价单!R3C9:R3C10",
                       "reason": "材料栏只有料重 28 与料价 0.96，没有材料费金额，不做乘法"}]
}
```

# 负例 3：表头区的词不是条目（真实事故）

某报价单第 6 行是表头区（该行还混着地址、日期等抬头信息），其中某列写着"镭雕"；
真正印出金额的数据行里，这一列是空的（或印着"/"），只有别的列（如"破氧白 0.40"）有金额。

❌ 错误示范：凭表头区的词造条目并编一个金额

```json
{"name": "镭雕", "amount_per_pc": 0.75, "confidence": "low",
 "evidence": {"file": "报价单", "location": "报价单!R7C17", "raw_text": "镭雕"}}
```

问题：0.75 在任何单元格里都不存在（编造值）；引用的 R7C17 原文其实是"破氧白"；
raw_text 抄的是表头区的词，与 location 指向的单元格不一致。

✅ 正确做法：表头区的词只用来理解列含义；数据行该列为空就不建条目（或该列金额印出来时按数据行建条目）

```json
{"name": "破氧白", "amount_per_pc": 0.40, "confidence": "high",
 "evidence": {"file": "报价单", "location": "报价单!R7C17:R9C17", "raw_text": "破氧白 0.40"}}
```

（"镭雕"这一列数据行为空 → 不生成条目，也不得凭空给金额。）
