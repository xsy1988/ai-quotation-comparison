<!-- version: v2.4.0 -->

把报价单解析为 JSON：顶层为 {"offers": [...]}，每个 offer 是一份完整报价，严格符合 quote_schema v1.1（压缩版字段说明）。offers 按"产品 × 报价方案"拆分：
1. 同一产品出现多行不同工艺路线/价格（多个报价方案）时，每行一个 offer，basic.scheme 填方案标识（取自原表方案列/品名/备注或工艺路线简述，如"方案1"）；
2. 报价单含多个不同产品时，每个产品每个方案一个 offer，basic.part_name 填部品名称；
3. 普通单产品单方案报价单：offers 只含 1 个元素。

模治具费用归属：能按部品名称/备注对应到具体 offer 的放入该 offer；整单共用的放入每个相关 offer，并在条目 note 标注"共用"。

金额一律照抄，禁止任何算术（最常翻车的地方，逐条自查）：
1. amount_per_pc / amount / rate 只能是单据上**已经印出来**的数字原样照抄；禁止相乘、相加、相减、
   摊薄、按单价×数量补齐，也禁止拿同列其它行、相邻单元格或模块合计反推补齐。
2. 单据只印出计算要素、没有印出最终金额时（例如材料栏只给"料重 28.5""料价 0.96"，整行没有材料费金额列）
   → amount_per_pc 填 null，把要素原文照抄进该条目 note，并把该路径列入 _self_check.null_fields。
   例：料重 28.5 × 料价 0.96 不得写成 27.36 或 26.88，只能 amount_per_pc=null、note="料重 28.5；料价 0.96"。
3. 单据某项只有费率没有金额时（如只有"税率13%"）→ amount_per_pc 填 null、rate 照抄费率。
4. 表头、标题、栏目名、说明文字都不是金额出处：那里的文字和数字不得当作任何条目的金额或 name。

每个 offer 的字段（required 标*）：
- schema_version: "1.1"
- supplier*: {supplier_name*: 字符串, supplier_code: null}
- basic*: {project_name: 字符串|null, part_name*: 字符串, scheme: 字符串|null（多方案时填方案标识，单方案填 null）,
  material_spec: 字符串|null,
  quote_date: "YYYY-MM-DD"|null, currency*: 枚举(CNY/USD/EUR/JPY/HKD/TWD/KRW/OTHER，判断不了填CNY),
  moq: 数字|null, category: 品类编码|null（见下方品类清单）, quote_no: 字符串|null,
  source_file: 字符串|null, parse_status: "parsed"}
- unit_price*: {materials*, processing*, inspection*, packaging_transport*, sga_tax*, other*, summary*}
- tooling: 对象|null（无模治具费用时填 null）
- other_info: 字符串|null（「其它信息」，见下方说明；无额外信息填 null）

金额单位：unit_price 下所有金额 = 元/pcs；tooling 下 = 元/项（一次性费用）。
六费用模块结构均为 {total: 数字|null, items: [...]}。供应商只报模块总价时 total 填数、items 为空数组。
items 里的 amount_per_pc（tooling 为 amount）允许为 null：只有单据确实没印出该金额时才填 null
（见上方"金额一律照抄"第 2、3 条），能照抄到数字时一律照抄真实数字，不要为了省事填 null。

各模块 items 字段（required 标*）：
- materials: {name*, amount_per_pc*, spec: 字符串|null, note: 字符串|null, evidence}
  （name 必须用材料的具体名称/牌号，如"ADC12铝合金"，与 basic.material_spec 保持一致；
  不要用"原材料/材料费/材料"这类栏目名；
  材料栏只给"料重/用料""料价/单价"等要素、整行没印出材料费金额时：amount_per_pc 填 null，
  把要素原文照抄进 note，禁止做乘法算材料费）
- processing: {name*, amount_per_pc*, atom_code: null, is_new_process: false, bundle_flag: false,
  confidence: "low", match_path: null, confirm_status: "unconfirmed", note: 字符串|null, evidence}
  （工艺原子映射是后续阶段的事：atom_code 一律 null、confidence 一律 "low"、match_path 一律 null；
  bundle_members/bundle_fingerprint/split_method 本次不要输出该字段）
- inspection: {name*, amount_per_pc*, note: 字符串|null, evidence}
- packaging_transport: {name*, amount_per_pc*, item_type*: "包装"|"运输", note: 字符串|null, evidence}
- sga_tax: {name*, amount_per_pc*, item_type*: "损耗"|"管理费"|"利润"|"税费"|"其他",
  rate: 数字|null（费率小数，0.13=13%；税费条目必填）, note: 字符串|null, evidence}
  （注意：某科目只有费率没有金额时 amount_per_pc 填 null，禁止借用相邻单元格金额，见下方负例；
  税费条目尤其如此——单据未直接印出税额数字时 amount_per_pc 必须为 null，严禁填 0，
  rate 照常识别，税额由下游脚本按 税率×未税 派生）
- other: {name*, amount_per_pc*, note*: 字符串（必填，说明这是什么费用）, evidence}

evidence（每个条目必填）: {"file": 源文件名, "location": "sheet名!单元格范围"（如 "报价单!B9:C9"）, "raw_text": 原文片段}
  （raw_text 必须是所引用单元格里**逐字照抄**的原文文本：不得改写、概括、拼接别处的文字；
  raw_text 里必须出现该条目 amount_per_pc 和 rate 的数字（税费条目只印出税率时除外）；
  一个金额由多个单元格共同支撑时，把每个单元格的原文片段用 " / " 依次列出（如 "料重 28.5 / 料价 0.96"）；
  严禁把坐标、行列号或 IR 序列化片段当 raw_text——形如 "8:1|9:28|10:0.96"、"R7C17"、"9:28" 的写法
  一律视为编造出处；也严禁把表头文字当金额出处（数据行该列为空却写 raw_text="镭雕"）；
  location 的 sheet 名必须严格使用 IR 序列化中每行行首那个 sheet 名原样照抄——
  例如 IR 行以 "CNC5分钟 (2)|9|..." 开头，坐标就写 "CNC5分钟 (2)!R9C3"；
  严禁用 "sheet"、"Sheet"、"工作表" 之类的占位词代替真实 sheet 名）

summary*: {untaxed_total: 数字|null, tax_amount: 数字|null, taxed_total: 数字|null,
  discount: 数字|null, final_unit_price_taxed*: 数字, calc_check: "unchecked"}
勾稽规则：未税合计 = Σ各模块合计（排除税费）；含税合计 = 未税合计 + 税额；最终含税单价 = 含税合计 − 折扣。
按此规则计算并填 summary；算不准时 final_unit_price_taxed 必须给最优估计值。

tooling: {total: 数字|null, molds/fixtures/stencils: {total: 数字|null, items: [
  {name*, amount*, cavities: 整数|null（穴数）, lifespan: 数字|null（寿命模次）, note: 字符串|null, evidence}]}}
模具→molds，治具/检具/夹具→fixtures，钢网/网板→stencils。

other_info（「其它信息」）—— 兜住 schema 装不下的信息，其它字段填完后必须再过一遍原文：
把报价单上**已识别但无处安放**的内容用 Markdown 记下来，供后续 AI 分析当补充信息源使用：
- 值得记录：材质/牌号说明、工艺流程与工序顺序描述、表面处理/检验标准、模具穴数与模次寿命、
  起订量/交期/账期/付款方式、运费与运输责任约定、报价有效期、税率与开票说明、
  良率/损耗/管理费/利润的口径说明、样品与量产差异、包装方式、调价条款、
  认证/合规说明、报价备注与风险提示、付款节点、模具费分摊或返还约定等。
- 组织方式：用 Markdown 小标题（## / ###）+ 列表或表格分类整理，例如
  `## 商务条款`、`## 工艺与质量`、`## 备注`；每条尽量带上原文数值。
- 严格禁止写入：手机号、联系人姓名、邮箱、微信号、传真、印章/签名等个人身份与签章信息，
  以及银行账号、开户行、开户资料等收款与开户信息；
  这类内容必须整条剔除，不得以任何变形（部分打码、缩写）形式保留。
- 与结构化字段重复的金额不必抄写，只补充结构化字段无法表达的上下文与条件。
- 确实没有任何额外信息时填 null，不要写"无"之类的占位文字。

输出契约：除 schema 字段外，每个 offer 顶层附一个 _self_check 对象（不进 schema，但必须输出）：
```json
"_self_check": {
  "amounts_traceable": true,
  "no_invented_values": true,
  "tax_amount_null_if_unprinted": true,
  "locations_use_ir_sheet_names": true,
  "other_info_excludes_sensitive_data": true,
  "null_fields": ["unit_price.sga_tax.items[3].amount_per_pc"],
  "uncertain_cells": [{"location": "...", "reason": "..."}]
}
```
- amounts_traceable: 每个金额/费率是否都能在 evidence.raw_text 中找到原文；
- no_invented_values: 是否没有任何编造值——含自己相乘/相加/相减/摊薄算出来的数字
  （例：单据只有料重 28.5 与料价 0.96，却写出 27.36/26.88 这类乘积，就是编造值，
  正确做法是 amount_per_pc 填 null 并把要素写进 note）；
- other_info_excludes_sensitive_data: other_info 是否已完全剔除手机号/姓名/邮箱/印章等个人与签章信息，
  以及银行账号/开户行/开户资料等收款与开户信息；
- tax_amount_null_if_unprinted: 税费条目自查——单据只有税率、未直接印出税额数字时，
  amount_per_pc 是否已填 null（而非 0 或任何推测值），rate 是否照常识别；
- locations_use_ir_sheet_names: 所有 location 的 sheet 名是否都是 IR 行首字段的原样 sheet 名
  （占位词如 "sheet"/"Sheet1"/"工作表" 一律视为编造值）；
- null_fields: 填了 null 的字段路径列表（缺失即 null 的落实）；
- uncertain_cells: 看不懂/不确定的单元格及原因。

负例警示（真实案例，务必遵守）：
{fewshot_negative}

品类判定：根据 part_name / material_spec / 报价内容判断零件所属品类，basic.category 填下方清单中的编码，都拿不准填 null：
{categories}

报价单原文（IR 格式：sheet|行号|列号:值|列号:值...）：
（窄列竖排的表头可能仍被拆到连续多个物理行：同一列号上下相邻的文本碎片属于同一列表头，
先拼成完整列名再对应到下方数据行，不要把碎片当作独立科目，金额以表体数据行为准。）
（表头区——标题行、表头行、悬浮说明行——里的词只是列名提示，不是条目：某个词只出现在表头区、
而数据行对应列是空的（空 / "/" / "——"）时，不得为它建条目、更不得给它编金额；只有数据行该列
确实印出数字时，才按该单元格建条目并照抄金额。）
（同一个词在表头区与数据区都出现（如"镭雕"既是列标题、数据行也印了金额）时，以数据行单元格为准，
raw_text 必须取自数据行，禁止用表头单元格当出处。）
{ir_serialized}

只输出一个 JSON 对象，不要输出任何其他文字，不要用 markdown 代码块包裹。

输出示例（结构示意，字段以实际内容为准；普通报价单 offers 只有 1 个元素）：
{"offers":[{"schema_version":"1.1","supplier":{"supplier_name":"XX公司","supplier_code":null},
"basic":{"project_name":null,"part_name":"某零件","scheme":null,"material_spec":null,"quote_date":"2026-09-01",
"currency":"CNY","moq":null,"category":"CAT-WJWK","quote_no":null,"source_file":"报价单.xlsx","parse_status":"parsed"},
"unit_price":{"materials":{"total":1.0,"items":[{"name":"铝材","amount_per_pc":1.0,"spec":null,"note":null,
"evidence":{"file":"报价单.xlsx","location":"Sheet1!B2:C2","raw_text":"铝材 1.0"}}]},
"processing":{"total":null,"items":[]},"inspection":{"total":null,"items":[]},
"packaging_transport":{"total":null,"items":[]},"sga_tax":{"total":null,"items":[]},
"other":{"total":null,"items":[]},
"summary":{"untaxed_total":null,"tax_amount":null,"taxed_total":null,"discount":null,
"final_unit_price_taxed":1.0,"calc_check":"unchecked"}},"tooling":null,
"other_info":"## 商务条款\n- 月结60天，报价有效期30天"}]}
