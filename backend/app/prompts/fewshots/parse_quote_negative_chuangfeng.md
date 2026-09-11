<!-- version: v1.1.0 -->

# 负例：惠州市创锋科技有限公司报价单（税率列只有费率、没有税额）

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
